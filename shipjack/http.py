"""The merchant's ACP Agentic Checkout REST API, with the AP2 additions, as a FastAPI app.

Follows the ACP checkout RFC where it is specific: ``Authorization: Bearer`` and ``API-Version`` on
every request, ``Idempotency-Key`` on every POST with replay of the stored response (same key, same
body), 422 ``idempotency_conflict`` (same key, different body), no caching of 5xx responses, keys
scoped to the caller and the path, flat error objects, 201 on create, 405 on cancelling a finished
session.

AP2 additions (this project's convention; AP2 leaves the commerce-protocol binding open):
``POST /checkout_sessions/{id}/ap2`` returns the merchant-signed checkout JWT and a fresh nonce, and
``GET /.well-known/ap2-merchant-key`` publishes the key that signs checkout JWTs.

After the order (this project's convention; ACP defines the order object and its webhooks, not these
routes): ``GET /orders/{id}`` returns the ACP Order to the platform. Moving money and goods is the
merchant's job, so refund, fulfill, deliver, cancel and dispute evidence take an *operator* token, and
an agent platform's token gets 403 there.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .mandates import FULFILLMENT
from .merchant import ACP_API_VERSION, CheckoutError, Merchant
from .psp import PaymentError

SUPPORTED_VERSIONS = [ACP_API_VERSION]
log = logging.getLogger("shipjack.http")


def _error(status: int, code: str, message: str, param: str | None = None, type_: str = "invalid_request",
           **extra: Any) -> JSONResponse:
    body = {"type": type_, "code": code, "message": message, **({"param": param} if param else {}), **extra}
    return JSONResponse(body, status_code=status)


def _canonical(value: Any) -> Any:
    """ACP request equivalence: key order and trailing zeros do not matter; null and absent differ."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _fingerprint(body: Any) -> str:
    return hashlib.sha256(json.dumps(_canonical(body), separators=(",", ":")).encode()).hexdigest()


def create_app(merchant: Merchant, tokens: dict[str, str], *, operators: dict[str, str] | None = None,
               kb=None) -> FastAPI:
    """``tokens`` maps bearer tokens to caller identities (one per agent platform); ``operators`` maps
    the merchant's own staff or back-office tokens, which alone may move money or goods after an order."""
    app = FastAPI(title=f"{merchant.info['name']} agentic checkout", version=ACP_API_VERSION)
    operators = dict(operators or {})
    if kb is None:
        kb = default_knowledge(merchant)
    app.state.kb = kb

    async def guarded(request: Request, status: int, action, *, operator: bool = False) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        presented = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        caller = None
        if presented is not None:
            if presented in operators:
                caller = f"operator:{operators[presented]}"
            elif presented in tokens:
                caller = tokens[presented]
                if operator:
                    return _error(403, "forbidden", "this action needs a merchant operator token")
        if caller is None:
            return _error(401, "unauthorized", "a valid Authorization: Bearer token is required")
        version = request.headers.get("api-version")
        if not version:
            return _error(400, "missing_api_version", "API-Version header is required",
                          supported_versions=SUPPORTED_VERSIONS)
        if version not in SUPPORTED_VERSIONS:
            return _error(400, "unsupported_api_version", f"API-Version {version} is not supported",
                          supported_versions=SUPPORTED_VERSIONS)
        if request.method != "POST":
            try:
                code, payload = status, action(None)
            except Exception as exc:  # noqa: BLE001 - mapped to a flat error below
                code, payload = _failure(request, exc)
                if code is None:
                    return payload
            return _respond(request, code, payload, {})
        raw = await request.body()
        try:
            body: Any = json.loads(raw) if raw else {}
        except ValueError:
            return _error(400, "invalid", "request body is not valid JSON")
        if not isinstance(body, dict):
            return _error(400, "invalid", "request body must be a JSON object")
        key = request.headers.get("idempotency-key")
        if not key:
            return _error(400, "idempotency_key_required", "Idempotency-Key header is required on all POST requests")
        if len(key) > 255:
            return _error(400, "invalid", "Idempotency-Key is longer than 255 characters")
        slot = (caller, request.url.path, key)
        try:
            # the replay check, the action and remembering its response are one transaction, so two
            # server processes that get the same key at the same moment cannot both act on it
            with merchant.repo.transaction():
                seen = merchant.repo.idempotent(slot)
                if seen is not None:
                    fingerprint, code, payload = seen
                    if fingerprint != _fingerprint(body):
                        return _error(422, "idempotency_conflict",
                                      "Idempotency-Key has already been used with a different request body")
                    return JSONResponse(payload, status_code=code,
                                        headers={"Idempotent-Replayed": "true", "Idempotency-Key": key})
                try:
                    code, payload = status, action(body)
                except (CheckoutError, _NotAllowed) as exc:
                    code, payload = _failure(request, exc)
                merchant.repo.remember(slot, _fingerprint(body), code, payload)
        except Exception:  # a fault rolls the change back, is a flat 500, and is never remembered
            log.exception("unhandled error in %s %s", request.method, request.url.path)
            return _error(500, "internal_error", "the request could not be processed", type_="processing_error")
        return _respond(request, code, payload, {"Idempotency-Key": key})

    @app.get("/.well-known/acp.json")
    def discovery() -> dict:
        """ACP discovery: who the seller is, the API versions it speaks, and the transports it offers.

        An agent reads this before its first call to choose REST or MCP and a compatible version.
        """
        return {"seller": dict(merchant.info), "protocol": {"supported_versions": SUPPORTED_VERSIONS},
                "transports": ["rest", "mcp"], "payment": {"handlers": ["card", "ap2", "direct"]},
                "extensions": [FULFILLMENT]}

    @app.get("/.well-known/ap2-merchant-key")
    def merchant_key() -> dict:
        return {"merchant": merchant.info, "jwk": json.loads(merchant.verification_key().export_public())}

    @app.get("/products")
    async def search(request: Request, q: str = ""):
        from .mcp import search_products
        return await guarded(request, 200, lambda _: {"data": search_products(merchant, kb, q)})

    @app.get("/products/{product_id}")
    async def product(request: Request, product_id: str):
        return await guarded(request, 200, lambda _: merchant.product(product_id))

    @app.post("/checkout_sessions")
    async def create(request: Request):
        return await guarded(request, 201, lambda b: merchant.create_session(
            b.get("items"), fulfillment_details=b.get("fulfillment_details"), buyer=b.get("buyer")))

    @app.post("/checkout_sessions/{session_id}")
    async def update(request: Request, session_id: str):
        return await guarded(request, 200, lambda b: merchant.update_session(
            session_id, items=b.get("items"), fulfillment_details=b.get("fulfillment_details"),
            buyer=b.get("buyer")))

    @app.get("/checkout_sessions/{session_id}")
    async def retrieve(request: Request, session_id: str):
        return await guarded(request, 200, lambda _: merchant.get_session(session_id))

    @app.post("/checkout_sessions/{session_id}/complete")
    async def complete(request: Request, session_id: str):
        return await guarded(request, 200, lambda b: merchant.complete_session(session_id, b.get("payment_data")))

    @app.post("/checkout_sessions/{session_id}/cancel")
    async def cancel(request: Request, session_id: str):
        def act(_):
            session = merchant.sessions.get(session_id)
            if session is not None and session.status in ("completed", "canceled"):
                raise _NotAllowed(session.status)
            return merchant.cancel_session(session_id)
        return await guarded(request, 200, act)

    @app.post("/checkout_sessions/{session_id}/ap2")
    async def ap2(request: Request, session_id: str):
        return await guarded(request, 200, lambda _: merchant.ap2_checkout(session_id))

    # ---- MCP over Streamable HTTP: the same store as MCP tools, for agents that speak MCP -----------
    from .mcp import MCPStore
    mcp_by_caller: dict[str, MCPStore] = {}

    @app.post("/mcp")
    async def mcp(request: Request):
        """MCP's Streamable HTTP transport, answering every request with plain JSON (no SSE stream).

        The bearer token authenticates the caller, as on the REST API; idempotency keys in ``meta`` are
        scoped to that caller. A notification gets 202 and no body, as the transport requires.
        """
        auth = request.headers.get("authorization", "")
        presented = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
        caller = tokens.get(presented) or (f"operator:{operators[presented]}" if presented in operators else None)
        if caller is None:
            return _error(401, "unauthorized", "a valid Authorization: Bearer token is required")
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                status_code=400)
        store = mcp_by_caller.setdefault(caller, MCPStore(merchant, kb=kb))
        batch = message if isinstance(message, list) else [message]
        replies = []
        for item in batch:
            try:
                reply = store.handle(item) if isinstance(item, dict) else \
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
            except Exception:  # a fault is a JSON-RPC internal error that says nothing about the inside
                log.exception("unhandled MCP error")
                reply = {"jsonrpc": "2.0", "id": item.get("id"), "error": {"code": -32603, "message": "internal error"}}
            if reply is not None:
                replies.append(reply)
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies if isinstance(message, list) else replies[0])

    @app.get("/mcp")
    async def mcp_stream():
        return Response(status_code=405, headers={"Allow": "POST"})  # no server-initiated stream

    # ---- the store's knowledge: search for agents, onboarding for operators ----------------------------

    @app.get("/knowledge/search")
    async def knowledge_search(request: Request, q: str = "", kind: str | None = None, k: int = 4,
                               mode: str | None = None):
        def act(_):
            if kind not in (None, "policy", "product") or mode not in (None, "hybrid", "vector", "keyword"):
                raise CheckoutError("invalid", "kind must be policy or product; mode hybrid, vector or keyword")
            return {"data": [h.as_dict() for h in kb.search(q[:500], kind=kind, k=max(1, min(k, 20)), mode=mode)],
                    "vector": kb.vector}
        return await guarded(request, 200, act)

    @app.get("/knowledge/documents")
    async def knowledge_documents(request: Request):
        return await guarded(request, 200, lambda _: {"data": kb.documents(), "vector": kb.vector,
                                                       "embedder": kb.embed.name if kb.vector else "keyword only"},
                             operator=True)

    @app.post("/admin/catalog")
    async def import_catalog(request: Request):
        """Onboard a catalog: a Shopify product export or a simple CSV, in ``{"csv": "..."}``."""
        def act(body):
            import time

            from .knowledge import product_chunk
            from .onboarding import parse_catalog
            text = body.get("csv")
            if not isinstance(text, str) or not text.strip():
                raise CheckoutError("invalid", "send the CSV text as {\"csv\": \"...\"}", "$.csv")
            started = time.perf_counter()
            try:
                parsed = parse_catalog(text)
            except ValueError as exc:
                raise CheckoutError("invalid", str(exc), "$.csv") from None
            merchant.replace_catalog(parsed.products)
            indexed = kb.replace("product", [product_chunk(p) for p in parsed.products])
            return {"format": parsed.format, "imported": len(parsed.products), "indexed": indexed,
                    "skipped": parsed.skipped[:50], "seconds": round(time.perf_counter() - started, 2)}
        return await guarded(request, 200, act, operator=True)

    @app.post("/admin/documents")
    async def add_document(request: Request):
        """Add or replace a policy page: ``{"slug": "returns", "markdown": "## Returns ..."}``."""
        def act(body):
            import re
            import time

            from .knowledge import policy_chunks
            slug, text = body.get("slug"), body.get("markdown")
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
                raise CheckoutError("invalid", "slug must be lowercase letters, digits and dashes", "$.slug")
            if not isinstance(text, str) or not text.strip() or len(text) > 200_000:
                raise CheckoutError("invalid", "markdown must be 1 to 200,000 characters", "$.markdown")
            started = time.perf_counter()
            chunks = policy_chunks(text, slug)
            kb.replace("policy", chunks, doc=slug)
            return {"slug": slug, "chunks": len(chunks), "seconds": round(time.perf_counter() - started, 2)}
        return await guarded(request, 200, act, operator=True)

    # ---- after the order: status for the platform; refunds, fulfillment and evidence for operators ----

    @app.get("/orders/{order_id}")
    async def get_order(request: Request, order_id: str):
        return await guarded(request, 200, lambda _: merchant.acp_order(order_id))

    @app.post("/orders/{order_id}/refund")
    async def refund(request: Request, order_id: str):
        def act(body):
            amount = body.get("amount")
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
                raise CheckoutError("invalid", "amount must be a whole number of cents", "$.amount")
            try:
                return merchant.refund_order(order_id, amount)
            except PaymentError as exc:
                raise CheckoutError(exc.code, str(exc), "$.amount") from None
        return await guarded(request, 200, act, operator=True)

    @app.post("/orders/{order_id}/fulfill")
    async def fulfill(request: Request, order_id: str):
        def act(body):
            fields = {k: body[k] for k in ("carrier", "tracking_number", "tracking_url") if k in body}
            return merchant.fulfill_order(order_id, **fields)
        return await guarded(request, 200, act, operator=True)

    @app.post("/orders/{order_id}/deliver")
    async def deliver(request: Request, order_id: str):
        return await guarded(request, 200, lambda _: merchant.mark_delivered(order_id), operator=True)

    @app.post("/orders/{order_id}/cancel")
    async def cancel_order(request: Request, order_id: str):
        def act(_):
            try:
                return merchant.cancel_order(order_id)
            except PaymentError as exc:
                raise CheckoutError(exc.code, str(exc)) from None
        return await guarded(request, 200, act, operator=True)

    @app.get("/orders/{order_id}/evidence")
    async def evidence(request: Request, order_id: str):
        return await guarded(request, 200, lambda _: merchant.dispute_evidence(order_id), operator=True)

    return app


class _NotAllowed(Exception):
    pass


def default_knowledge(merchant: Merchant):
    """The store's knowledge base, filled with its catalog and the bundled policies the first time.

    On Postgres it lives in the database (hybrid search) and is filled once; in memory or SQLite it is
    keyword-only and rebuilt at each start.
    """
    from .knowledge import KnowledgeBase, load_policies, policy_chunks, product_chunk
    db = getattr(merchant.repo, "db", None)
    kb = KnowledgeBase(merchant.id, db=db)
    if not kb.documents():
        kb.replace("product", [product_chunk(p) for p in merchant.products.values()])
        kb.replace("policy", policy_chunks(load_policies(), "northwind-policies"), doc="northwind-policies")
    return kb


def _failure(request: Request, exc: Exception):
    """An expected refusal as (status, body); anything else is logged and becomes a flat 500."""
    if isinstance(exc, CheckoutError):
        return (404 if exc.code == "not_found" else 400), exc.body()
    if isinstance(exc, _NotAllowed):
        return 405, {"type": "invalid_request", "code": "invalid", "message": f"session is already {exc}"}
    log.exception("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
    return None, _error(500, "internal_error", "the request could not be processed", type_="processing_error")


def _respond(request: Request, code: int, payload: Any, headers: dict) -> JSONResponse:
    if request.headers.get("request-id"):
        headers["Request-Id"] = request.headers["request-id"]
    return JSONResponse(payload, status_code=code, headers=headers)


def demo_app(token: str = "demo-token", *, stripe: bool = False, operator_token: str = "demo-operator",
             webhook_url: str | None = None, db: str | None = None) -> FastAPI:
    """A ready-to-run store (Northwind) with one demo platform token, for ``shipjack-serve``.

    With ``stripe=True`` the money settles through Stripe test mode (needs STRIPE_TEST_KEY and the
    'stripe' extra); otherwise the processor is simulated. With ``webhook_url``, order events are
    signed with SHIPJACK_WEBHOOK_SECRET and delivered to that receiver in the background. With
    ``db`` (a SQLite path) the store's state, the ledger and the webhook outbox live in that file,
    survive a restart, and can be shared by several server processes.
    """
    from .scenarios import stores
    from .world import build
    processor = None
    if stripe:
        from .stripe_psp import StripeProcessor
        processor = StripeProcessor.from_env()
    outbox = None
    if webhook_url:
        from .webhooks import Outbox, secret_from_env
        outbox = Outbox(webhook_url, secret_from_env())
    world = build(stores(), processor=processor, db=db)
    merchant = world.merchants["northwind"]
    app = create_app(merchant, {token: "demo-agent-platform"}, operators={operator_token: "demo-operator"})
    app.state.world = world
    app.state.outbox = outbox.attach(merchant).start() if outbox else None
    return app


def app_from_env() -> FastAPI:
    """The demo store configured from SHIPJACK_* variables: what each worker process builds."""
    import os
    env = os.environ.get
    return demo_app(env("SHIPJACK_TOKEN", "demo-token"), stripe=env("SHIPJACK_STRIPE") == "1",
                    operator_token=env("SHIPJACK_OPERATOR_TOKEN", "demo-operator"),
                    webhook_url=env("SHIPJACK_WEBHOOK_URL") or None, db=env("SHIPJACK_DB") or None)


def main(argv: list[str] | None = None) -> int:
    """Run the demo store over HTTP (the REST transport). Needs the optional 'serve' extra (uvicorn)."""
    import argparse
    parser = argparse.ArgumentParser(prog="shipjack-serve", description="Run the demo agent-ready store over HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token", default="demo-token", help="the bearer token an agent platform presents")
    parser.add_argument("--stripe", action="store_true", help="settle through Stripe test mode (needs STRIPE_TEST_KEY)")
    parser.add_argument("--webhook-url", help="send signed ACP order events here (secret: SHIPJACK_WEBHOOK_SECRET)")
    parser.add_argument("--db", help="keep the store's state in a SQLite file or a postgresql:// URL (or set SHIPJACK_DB)")
    parser.add_argument("--workers", type=int, default=1, help="server processes sharing --db (default 1)")
    args = parser.parse_args(argv)
    import os
    if args.workers > 1 and not (args.db or os.environ.get("SHIPJACK_DB")):
        import sys
        print("shipjack-serve: --workers above 1 needs --db, so the processes share one store", file=sys.stderr)
        return 2
    try:
        import uvicorn
    except ImportError:
        import sys
        print("shipjack-serve needs uvicorn: pip install 'shipjack[serve]'", file=sys.stderr)
        return 2
    import os
    os.environ.update({"SHIPJACK_TOKEN": args.token, "SHIPJACK_STRIPE": "1" if args.stripe else "",
                       "SHIPJACK_WEBHOOK_URL": args.webhook_url or os.environ.get("SHIPJACK_WEBHOOK_URL", ""),
                       "SHIPJACK_DB": args.db or os.environ.get("SHIPJACK_DB", "")})
    args.db = os.environ["SHIPJACK_DB"] or None
    try:
        app = app_from_env()  # also checks the configuration before any worker starts
    except ValueError as exc:
        import sys
        print(f"shipjack-serve: {exc}", file=sys.stderr)
        return 2
    rail = "Stripe test mode" if args.stripe else "simulated processor"
    hooks = f"; order webhooks to {args.webhook_url}" if args.webhook_url else ""
    import re
    shown = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", args.db or "")  # never print a database password
    store = f"; state in {shown}" + (f", {args.workers} workers" if args.workers > 1 else "") if args.db else ""
    print(f"shipjack store on http://{args.host}:{args.port}  ({rail}{hooks}{store}; "
          "ACP discovery at /.well-known/acp.json)", flush=True)
    if args.workers > 1:
        if app.state.outbox:
            app.state.outbox.stop()
        uvicorn.run("shipjack.http:app_from_env", factory=True, host=args.host, port=args.port, workers=args.workers)
    else:
        uvicorn.run(app, host=args.host, port=args.port)
    return 0
