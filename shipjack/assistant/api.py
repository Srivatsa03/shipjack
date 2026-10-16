"""The assistant's HTTP API: what the web app (shopper chat) and the operator console call.

A turn streams as server-sent events while the agents work: ``token`` (agent text as it is written),
``trace`` (model calls, tool calls, checks, with tokens and latency), ``node`` (each step's result),
``approval`` (the graph paused for a person), ``done``. The same events are stored, so the console can
replay any run.

People in the loop:
* the **shopper** approves or declines a purchase card (``POST /api/threads/{id}/decision``);
* a **store operator** approves or declines a refund (``POST /api/ops/approvals/{id}``); the paused
  conversation then resumes in the background.

The store's signed order webhooks arrive at ``POST /api/webhooks/orders`` and keep each order's
status current (signature checked, duplicates and older snapshots ignored).

Configuration comes from the environment; see ``Settings``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessageChunk
from langgraph.types import Command

from ..catalog import address_problems
from ..webhooks import SIGNATURE_HEADER, SignatureError, _event_problem, verify
from .db import AssistantDB
from .graph import Deps, build_graph, make_messages
from .models import text_of

log = logging.getLogger("shipjack.assistant")
AGENT_NODES = ("shopper", "support", "chat")
DEMO_CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}


@dataclass
class Settings:
    db_url: str = ""                                   # Postgres; the assistant uses its own schema in it
    store_url: str = "http://127.0.0.1:8000"
    store_token: str = "demo-token"                    # the assistant's token at the store's ACP API
    operator_token: str = "demo-operator"              # the store operator token used to execute approved refunds
    webhook_secret: bytes = b""                        # shared with the store; empty disables the webhook route
    origins: list[str] = field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    schema: str = "assistant"
    purchase_ttl_s: float = 15 * 60                    # a purchase card can be approved for this long
    allow_imports: bool = True                         # catalog and document imports (off in the public demo)
    demo: bool = False                                 # public demo: a private sandbox per visitor, daily caps
    demo_calls_per_guest: int = 40                     # model calls per visitor per day
    demo_calls_per_day: int = 600                      # model calls per day for the whole demo
    demo_guests_per_ip_hour: int = 6
    sweep_every_s: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        demo = env("SHIPJACK_DEMO", "") == "1"
        return cls(db_url=env("SHIPJACK_ASSISTANT_DB") or env("SHIPJACK_DB", ""), demo=demo, allow_imports=not demo,
                   demo_calls_per_guest=int(env("SHIPJACK_DEMO_CALLS_PER_GUEST", "40")),
                   demo_calls_per_day=int(env("SHIPJACK_DEMO_CALLS_PER_DAY", "600")),
                   store_url=env("SHIPJACK_STORE_URL", cls.store_url),
                   store_token=env("SHIPJACK_STORE_TOKEN") or env("SHIPJACK_TOKEN", cls.store_token),
                   operator_token=env("SHIPJACK_OPERATOR_TOKEN", cls.operator_token),
                   webhook_secret=env("SHIPJACK_WEBHOOK_SECRET", "").encode(),
                   origins=[o for o in env("SHIPJACK_WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
                            .split(",") if o])


def _with_schema(url: str, schema: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}options=-csearch_path%3D{schema}"


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def message_view(m) -> dict:
    """A message as the web app shows it."""
    out = {"id": m.id, "role": {"human": "user", "ai": "assistant", "tool": "tool"}.get(m.type, m.type),
           "content": text_of(m.content)}
    if getattr(m, "tool_calls", None):
        out["tool_calls"] = [{"name": c["name"], "args": c["args"]} for c in m.tool_calls]
    if m.type == "tool":
        out["name"] = getattr(m, "name", None)
        out["content"] = out["content"][:2000]
    return out


def _update_view(update: Any) -> dict:
    if not isinstance(update, dict):
        return {}
    view: dict = {}
    msgs = update.get("messages") or []
    if msgs:
        view["messages"] = [message_view(m) for m in msgs]
    for key in ("route", "checkout", "order_id", "refund"):
        if update.get(key) is not None:
            view[key] = update[key]
    return view


def create_app(settings: Settings | None = None, *, deps: Deps | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.db_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("the assistant needs Postgres: set SHIPJACK_ASSISTANT_DB to a postgresql:// URL")
    state: dict[str, Any] = {"locks": {}, "tasks": set()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import psycopg
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        async with await psycopg.AsyncConnection.connect(settings.db_url, autocommit=True) as conn:
            await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.schema}"')
        pool = AsyncConnectionPool(_with_schema(settings.db_url, settings.schema), open=False, max_size=10,
                                   kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        db = AssistantDB(pool)
        await db.setup(seed=not settings.demo)  # the public demo has no shared, well-known tokens
        nonlocal deps
        if deps is None:
            deps = await _default_deps(settings, db)
        else:
            deps.orders_for, deps.record_order = db.order_ids, _recorder(db)
        state.update(db=db, graph=build_graph(deps, checkpointer=saver), deps=deps)
        sweeper = asyncio.create_task(sweep())
        try:
            yield
        finally:
            sweeper.cancel()
            for task in list(state["tasks"]):
                task.cancel()
            await pool.close()

    app = FastAPI(title="shipjack assistant", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_methods=["*"], allow_headers=["*"])

    def db() -> AssistantDB:
        return state["db"]

    async def user(request: Request) -> dict:
        auth = request.headers.get("authorization", "")
        found = await db().user_by_token(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
        if found is None:
            raise HTTPException(401, "a valid Authorization: Bearer token is required")
        return found

    async def operator(u: dict = Depends(user)) -> dict:
        if u["role"] != "operator":
            raise HTTPException(403, "this needs a store operator")
        return u

    async def own_thread(thread_id: str, u: dict) -> dict:
        thread = await db().thread(thread_id)
        mine = thread is not None and (thread["user_id"] == u["id"] or
                                       (u["role"] == "operator" and u.get("sandbox") in (None, thread["user_id"])))
        if not mine:
            raise HTTPException(404, "no such thread")
        return thread

    def client_ip(request: Request) -> str:
        """The visitor's address. Behind the host's proxy it is the first X-Forwarded-For entry."""
        forwarded = request.headers.get("x-forwarded-for", "")
        return (forwarded.split(",")[0].strip() if forwarded else request.client.host if request.client else "?")[:64]

    # ---- running the graph ---------------------------------------------------------------------
    async def run(thread_id: str, payload: Any):
        """One run of the graph, as SSE strings. Every trace event is also stored."""
        graph, store = state["graph"], db()
        lock = state["locks"].setdefault(thread_id, asyncio.Lock())
        if lock.locked():
            yield _sse("error", {"message": "this conversation is already running"})
            return
        run_id = uuid.uuid4().hex[:12]
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        async with lock:
            yield _sse("start", {"run_id": run_id})
            try:
                async for mode, chunk in graph.astream(payload, config, stream_mode=["messages", "updates", "custom"]):
                    if mode == "messages":
                        msg, meta = chunk
                        node = meta.get("langgraph_node")
                        if node in AGENT_NODES and isinstance(msg, AIMessageChunk) and text_of(msg.content):
                            yield _sse("token", {"node": node, "text": text_of(msg.content)})
                    elif mode == "custom":
                        await store.add_event(thread_id, run_id, chunk.get("event", "trace"), chunk.get("node"), chunk)
                        yield _sse("trace", chunk)
                    elif mode == "updates":
                        for node, update in chunk.items():
                            if node == "__interrupt__":
                                for item in update:
                                    await store.add_approval(item.id, thread_id, item.value)
                                    await store.add_event(thread_id, run_id, "approval_requested", None,
                                                          {"id": item.id, **item.value})
                                    yield _sse("approval", {"id": item.id, **item.value})
                            else:
                                view = _update_view(update)
                                await store.add_event(thread_id, run_id, "node", node, view)
                                yield _sse("node", {"node": node, **view})
            except Exception as exc:  # the run failed; say so, keep the service up
                log.exception("run failed for thread %s", thread_id)
                await store.add_event(thread_id, run_id, "run_error", None, {"error": type(exc).__name__})
                yield _sse("error", {"message": "something went wrong; nothing was charged without your approval"})
            await store.touch(thread_id)
            yield _sse("done", {"run_id": run_id})

    async def sweep() -> None:
        """Expire purchase cards nobody answered in time, and cancel their checkouts (like a payment sheet
        that times out), so an old card can never be approved later."""
        while True:
            await asyncio.sleep(settings.sweep_every_s)
            try:
                for stale in await db().expire("purchase", settings.purchase_ttl_s):
                    await resume_in_background(stale["thread_id"], {"approved": False, "by": "expired"})
            except Exception:  # noqa: BLE001 - the sweeper must keep running
                log.exception("approval sweep failed")

    def stream(gen) -> StreamingResponse:
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def resume_in_background(thread_id: str, decision: dict) -> None:
        async def go():
            async for _ in run(thread_id, Command(resume=decision)):
                pass
        task = asyncio.create_task(go())
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)

    # ---- shopper routes ---------------------------------------------------------------------------
    @app.get("/api/health")
    async def health():
        return {"ok": True, "demo": settings.demo, "models": state["deps"].models.names if state.get("deps") else []}

    @app.post("/api/demo/session", status_code=201)
    async def demo_session(request: Request):
        """Public demo: a private sandbox for this visitor (a shopper and that shopper's store console)."""
        if not settings.demo:
            raise HTTPException(404, "not a public demo")
        ip = client_ip(request)
        if await db().guests_from(ip) >= settings.demo_guests_per_ip_hour:
            raise HTTPException(429, "Too many new demo sessions from this network. Try again in an hour.")
        return await db().create_guest(ip)

    @app.get("/api/me")
    async def me(u: dict = Depends(user)):
        return u

    @app.put("/api/me/address")
    async def set_address(request: Request, u: dict = Depends(user)):
        address = await request.json()
        problems = address_problems(address) if isinstance(address, dict) else ["the address must be an object"]
        missing = [k for k in ("name", "line_one", "city", "state", "postal_code") if not (address or {}).get(k)] \
            if isinstance(address, dict) else []
        if problems or missing:
            raise HTTPException(400, "; ".join(problems + [f"{k} is required" for k in missing]))
        await db().set_address(u["id"], address)
        return {**u, "address": address}

    @app.get("/api/threads")
    async def threads(u: dict = Depends(user)):
        return await db().threads(u["id"])

    @app.post("/api/threads", status_code=201)
    async def new_thread(u: dict = Depends(user)):
        return await db().create_thread(f"th_{uuid.uuid4().hex[:16]}", u["id"])

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str, u: dict = Depends(user)):
        thread = await own_thread(thread_id, u)
        snap = await state["graph"].aget_state({"configurable": {"thread_id": thread_id}})
        values = snap.values or {}
        pending = [{"id": i.id, **i.value} for t in snap.tasks for i in t.interrupts]
        return {**thread, "messages": [message_view(m) for m in values.get("messages", [])],
                "pending": pending, "orders": await db().orders(thread_id=thread_id)}

    @app.post("/api/threads/{thread_id}/messages")
    async def send(thread_id: str, request: Request, u: dict = Depends(user)):
        await own_thread(thread_id, u)
        body = await request.json()
        text = str(body.get("text", "")).strip() if isinstance(body, dict) else ""
        if not text or len(text) > 2000:
            raise HTTPException(400, "text must be 1 to 2000 characters")
        if not u.get("address"):
            raise HTTPException(400, "save a delivery address first")
        if settings.demo:
            if await db().model_calls_today(u["id"]) >= settings.demo_calls_per_guest:
                raise HTTPException(429, "You've used today's demo turns. Come back tomorrow, or run shipjack locally.")
            if await db().model_calls_today() >= settings.demo_calls_per_day:
                raise HTTPException(429, "The demo has used today's model budget. Please try again tomorrow.")
        await db().touch(thread_id, title=text)
        profile = {k: u[k] for k in ("id", "name", "email", "address")}
        return stream(run(thread_id, {**make_messages(text), "user": profile}))

    @app.post("/api/threads/{thread_id}/decision")
    async def decision(thread_id: str, request: Request, u: dict = Depends(user)):
        await own_thread(thread_id, u)
        body = await request.json()
        approval_id, approved = body.get("approval_id"), body.get("approved") is True
        pending = {a["id"]: a for a in await db().approvals(status="pending", thread_id=thread_id, actor="shopper")}
        if approval_id not in pending:
            raise HTTPException(404, "no pending approval with that id")
        if await db().decide(approval_id, approved, u["id"], max_age_s=settings.purchase_ttl_s) is None:
            raise HTTPException(409, "this approval was already decided or has expired; ask the assistant again")
        return stream(run(thread_id, Command(resume={"approved": approved, "by": u["id"]})))

    @app.get("/api/threads/{thread_id}/events")
    async def events(thread_id: str, after: int = 0, u: dict = Depends(user)):
        await own_thread(thread_id, u)
        return await db().events(thread_id, after)

    @app.get("/api/orders")
    async def my_orders(u: dict = Depends(user)):
        return await db().orders(user_id=u["id"])

    # ---- operator routes --------------------------------------------------------------------------
    @app.get("/api/ops/approvals")
    async def ops_approvals(status: str | None = "pending", op: dict = Depends(operator)):
        return await db().approvals(status=status or None, actor="operator", sandbox=op.get("sandbox"))

    @app.post("/api/ops/approvals/{approval_id}")
    async def ops_decide(approval_id: str, request: Request, op: dict = Depends(operator)):
        body = await request.json()
        approved, note = body.get("approved") is True, str(body.get("note", ""))
        if op.get("sandbox"):
            target = await db().approval(approval_id)
            if target is None or target["user_id"] != op["sandbox"]:
                raise HTTPException(404, "no such approval")
        found = await db().decide(approval_id, approved, op["id"], note)
        if found is None:
            raise HTTPException(409, "not pending (already decided, or no such approval)")
        if found["actor"] != "operator":
            raise HTTPException(403, "shoppers decide their own purchases")
        await resume_in_background(found["thread_id"], {"approved": approved, "note": note, "by": op["id"]})
        return JSONResponse({"id": approval_id, "status": "approved" if approved else "declined"}, status_code=202)

    @app.get("/api/ops/orders")
    async def ops_orders(op: dict = Depends(operator)):
        return await db().orders(user_id=op.get("sandbox"))

    @app.post("/api/ops/orders/{order_id}/{action}")
    async def ops_order_action(order_id: str, action: str, request: Request, op: dict = Depends(operator)):
        from .store_client import StoreError
        if op.get("sandbox") and await db().order_owner(order_id) != op["sandbox"]:
            raise HTTPException(404, "no such order")
        client = state["deps"].store
        key = f"ops:{action}:{order_id}:{uuid.uuid4().hex[:8]}"
        try:
            if action == "ship":
                body = await request.json() if await request.body() else {}
                return await client.fulfill(order_id, key, **{k: v for k, v in (body or {}).items()
                                                              if k in ("carrier", "tracking_number", "tracking_url")})
            if action == "deliver":
                return await client.deliver(order_id, key)
        except StoreError as exc:
            raise HTTPException(exc.status or 502, f"{exc.code}: {exc}") from None
        raise HTTPException(404, "action must be ship or deliver")

    # ---- onboarding: the store's catalog and knowledge, through the store's operator API -------------
    async def store_call(make):
        from .store_client import StoreError
        try:
            return await make(state["deps"].store)
        except StoreError as exc:
            raise HTTPException(exc.status if exc.status and exc.status < 500 else 502, f"{exc.code}: {exc}") from None

    @app.get("/api/ops/knowledge")
    async def ops_knowledge(_: dict = Depends(operator)):
        return {**await store_call(lambda s: s.documents()), "imports_allowed": settings.allow_imports}

    @app.get("/api/ops/knowledge/search")
    async def ops_search(q: str, kind: str | None = None, mode: str | None = None, _: dict = Depends(operator)):
        return await store_call(lambda s: s.search_docs(q, kind, mode))

    @app.get("/api/ops/sample-catalog")
    async def sample_catalog(_: dict = Depends(operator)):
        from importlib import resources
        text = resources.files("shipjack").joinpath("data/northwind_shopify_export.csv").read_text()
        return {"name": "northwind_shopify_export.csv", "csv": text}

    @app.post("/api/ops/catalog")
    async def ops_import(request: Request, _: dict = Depends(operator)):
        if not settings.allow_imports:
            raise HTTPException(403, "imports are off in the public demo; run shipjack locally to try them")
        body = await request.json()
        return await store_call(lambda s: s.import_catalog(str(body.get("csv", "")), key=f"import:{uuid.uuid4().hex}"))

    @app.post("/api/ops/documents")
    async def ops_document(request: Request, _: dict = Depends(operator)):
        if not settings.allow_imports:
            raise HTTPException(403, "imports are off in the public demo; run shipjack locally to try them")
        body = await request.json()
        return await store_call(lambda s: s.add_document(str(body.get("slug", "")), str(body.get("markdown", "")),
                                                         key=f"doc:{uuid.uuid4().hex}"))

    @app.get("/api/ops/threads")
    async def ops_threads(op: dict = Depends(operator)):
        scope, args = (" WHERE t.user_id = %s", (op["sandbox"],)) if op.get("sandbox") else ("", ())
        return await db()._all("SELECT t.id, t.title, t.updated, u.name AS user_name, "
                               "(SELECT count(*) FROM events e WHERE e.thread_id = t.id AND e.event = 'model_call') "
                               "AS model_calls FROM threads t JOIN users u ON u.id = t.user_id" + scope +
                               " ORDER BY t.updated DESC LIMIT 100", args)

    @app.get("/api/ops/metrics")
    async def ops_metrics(_: dict = Depends(operator)):
        return await db().metrics()

    # ---- the store's signed order webhooks ----------------------------------------------------------
    @app.post("/api/webhooks/orders")
    async def order_webhook(request: Request):
        if not settings.webhook_secret:
            return JSONResponse({"type": "service_unavailable", "code": "not_configured",
                                 "message": "webhooks are not configured"}, status_code=503)
        raw = await request.body()
        try:
            signed_at, signature = verify(settings.webhook_secret, raw, request.headers.get(SIGNATURE_HEADER))
        except SignatureError as exc:
            return JSONResponse({"type": "invalid_request", "code": "invalid_signature", "message": str(exc)},
                                status_code=401)
        try:
            event = json.loads(raw)
        except ValueError:
            return JSONResponse({"type": "invalid_request", "code": "invalid", "message": "not JSON"}, status_code=400)
        problem = _event_problem(event)
        if problem:
            return JSONResponse({"type": "invalid_request", "code": "invalid", "message": problem[1],
                                 "param": problem[0]}, status_code=400)
        request_id = request.headers.get("request-id")
        reply = {"received": True, **({"request_id": request_id} if request_id else {})}
        if await db().seen_webhook(signature, request_id, signed_at):
            return reply
        order = event["data"]
        if await db().apply_order_event(order, signed_at):
            owner = await db()._one("SELECT thread_id FROM orders WHERE id = %s", (order["id"],))
            if owner:
                await db().add_event(owner["thread_id"], "webhook", "order_update", None,
                                     {"order_id": order["id"], "status": order.get("status"), "type": event["type"]})
        return reply

    return app


def _recorder(db: AssistantDB):
    async def record(thread_id: str, user_id: str, order: dict) -> None:
        await db.add_order(order["id"], thread_id, user_id, order)
    return record


async def _default_deps(settings: Settings, db: AssistantDB) -> Deps:
    from .models import Models
    from .store_client import StoreClient, StoreMCP
    store = StoreClient(settings.store_url, settings.store_token, settings.operator_token)
    try:
        discovery = await store.discovery()
    except Exception:  # noqa: BLE001 - the store may start after the assistant; the seller check is skipped then
        log.warning("store discovery failed at %s; the merchant-id check is off until restart", settings.store_url)
        discovery = {}
    mcp = StoreMCP(settings.store_url.rstrip("/") + "/mcp", settings.store_token)
    return Deps(store=store, mcp=mcp, models=Models.from_env(), payment=DEMO_CARD, orders_for=db.order_ids,
                record_order=_recorder(db), policies=store_policies(mcp), discovery=discovery)


def store_policies(mcp):
    """The store's own policy search (its knowledge base, over MCP), for the support agent."""
    async def search(query: str) -> list[dict]:
        hits = (await mcp.call("search_store_docs", payload={"query": query, "kind": "policy"}))["data"]
        return [{"title": h["title"], "text": h["text"], "source": h["source"]} for h in hits[:3]]
    return search


def main(argv: list[str] | None = None) -> int:
    """Run the assistant API (``shipjack-assistant``)."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="shipjack-assistant", description="Run the shopping assistant API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    args = parser.parse_args(argv)
    try:
        import uvicorn
        app = create_app()
    except (ImportError, ValueError) as exc:
        print(f"shipjack-assistant: {exc}", file=sys.stderr)
        return 2
    print(f"shipjack assistant on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
