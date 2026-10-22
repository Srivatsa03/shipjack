"""Lost-response retry vectors for the store's MCP checkout tools, for MCP issue #3394.

A mutating ``tools/call`` can commit its effect and lose its response; the client then retries with a
fresh JSON-RPC id. These vectors run that against the store's real MCP endpoint (Streamable HTTP,
JSON answers), a SQLite database and the simulated payment processor, and keep three records apart:

- ``effects``: what the processor's ledger (or the session table) says happened. This is the oracle.
- ``observed``: what the client saw on each call, in order.
- ``reported``: what the client can conclude, once from the retry's answer alone (``from_answer``)
  and once after reading the session back with ``get_checkout_session`` (``after_read_back``).

Faults: in the in-process vectors the server commits and answers, and the client drops the answer
(it sees a timeout). In the ``K`` vectors the store runs in a child process that ends itself with
``os._exit`` right after the rail charged, or right before it would have; the client's connection
drops and a new process starts on the same database. One scripted application retry per step, never
an SDK retry.

What carries the identity: ``complete_checkout_session`` names a checkout session the server issued
in an earlier call, and a session is paid at most once. So even a retry with a new key, or none,
cannot pay twice; the key decides only whether the retry gets the original answer back.
``create_checkout_session`` has no such identity: without a key a retry makes a second session.

    shipjack mcp-retry-vectors --json docs/mcp_retry_vectors.json
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version

from .merchant import ACP_API_VERSION
from .scenarios import HOME, HUB, stores
from .world import build

CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}
BAD_CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4000000000000002"}}}
TOKENS = {"tok-a": "platform-a", "tok-b": "platform-b"}
UNKNOWN = ("payment_in_progress", "idempotency_in_flight")


class Agent:
    """An MCP client of one agent platform: one bearer token, a fresh JSON-RPC id per request."""

    def __init__(self, http, token: str = "tok-a"):
        self.http, self.token, self.rpc_id = http, token, 0
        self.ids: list[int] = []

    def call(self, tool: str, args: dict, key: str | None = None, *, drop: bool = False,
             rpc_id: int | None = None, **meta) -> dict:
        """One tools/call. ``drop``: the server answers and the client never sees it (a timeout)."""
        if rpc_id is None:
            self.rpc_id += 1
            rpc_id = self.rpc_id
        self.ids.append(rpc_id)
        meta = {"api_version": ACP_API_VERSION, **({"idempotency_key": key} if key else {}), **meta}
        message = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": {"meta": meta, **args}}}
        try:
            r = self.http.post("/mcp", json=message, headers={"Authorization": f"Bearer {self.token}"})
        except Exception:  # noqa: BLE001 - the server process died mid-call: the response is lost
            return {"lost": "connection dropped"}
        if drop:
            return {"lost": "timeout"}
        result = r.json()["result"]
        body = json.loads(result["content"][0]["text"])
        if result["isError"]:
            return {"error": body.get("code"), **({"retry_after": body["retry_after"]} if "retry_after" in body else {})}
        out = {"status": body.get("status"), "session": body.get("id")}
        if body.get("order"):
            out["order"] = body["order"]["id"]
        if body.get("error"):
            out["declined"] = body["error"].get("code")
        return out

    def session(self, key: str | None = None, *, drop: bool = False) -> dict:
        return self.call("create_checkout_session", {"payload": {
            "items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": HOME}}}, key, drop=drop)

    def pay(self, sid: str, key: str | None = None, card: dict = CARD, **kw) -> dict:
        return self.call("complete_checkout_session", {"id": sid, "payload": {"payment_data": card}}, key, **kw)

    def read_back(self, sid: str) -> dict:
        return self.call("get_checkout_session", {"id": sid})


def from_answer(answer: dict) -> str:
    """What a client concludes from a complete call's answer alone."""
    if "lost" in answer:
        return "unknown"
    if answer.get("error") in UNKNOWN:
        return "unknown"
    if answer.get("error") or answer.get("declined"):
        return "failed"
    return "succeeded" if answer.get("status") == "completed" else "unknown"


def from_read_back(session: dict) -> str:
    """What a client concludes from the session's state. ``ready_for_payment`` is authoritative here:
    an attempt is marked before the rail is called and cleared only once it is recorded or proven dead."""
    return {"completed": "succeeded", "complete_in_progress": "unknown",
            "ready_for_payment": "not paid"}.get(session.get("status"), "unknown")


# ---- the store, in process ---------------------------------------------------------------------------


@contextmanager
def store(db: str | None = None, psp_delay_s: float = 0.0):
    """A store on a SQLite file, with its MCP endpoint; yields (world, merchant, http client)."""
    from fastapi.testclient import TestClient

    from .http import create_app
    folder = None
    if db is None:
        folder = tempfile.TemporaryDirectory()
        db = os.path.join(folder.name, "store.db")
    world = build(stores(), db=db)
    world.processor.settle_delay_s = psp_delay_s
    merchant = world.merchants["northwind"]
    try:
        yield world, merchant, TestClient(create_app(merchant, TOKENS)), db
    finally:
        if folder is not None:
            folder.cleanup()


@contextmanager
def held_rail(world):
    """The rail does not answer until the returned event is set: the payment is in flight, for as long
    as the case needs, with no sleeps to tune."""
    release, real = threading.Event(), world.processor._settle

    def settle(*args, **kwargs):
        release.wait(30)
        return real(*args, **kwargs)

    world.processor._settle = settle
    try:
        yield release
    finally:
        release.set()
        world.processor._settle = real


def wait_marked(merchant, sid: str) -> None:
    """Until the payment attempt is marked (the rail is being called)."""
    deadline = time.time() + 30
    while not merchant.sessions[sid].paying:
        if time.time() > deadline:
            raise RuntimeError("the payment never started")
        time.sleep(0.01)


def charges(world, sid: str) -> int:
    return sum(1 for c in world.processor.charges if c.reference == sid)


def vector(vid: str, rows: str, title: str, effects: dict, observed: list, reported: dict, expected: dict,
           note: str = "") -> dict:
    """``expected`` names effects and conclusions; a tuple accepts any of its values."""
    seen = {**effects, **reported}
    ok = all(seen.get(k) in (want if isinstance(want, tuple) else (want,)) for k, want in expected.items())
    return {"id": vid, "matrix": rows, "title": title, "effects": effects, "observed": observed,
            "reported": reported, "expected": {k: list(v) if isinstance(v, tuple) else v for k, v in expected.items()},
            "as_expected": ok, "note": note}


def lost_complete(vid, rows, title, retry_key, answer, note=""):
    with store() as (w, m, http, _):
        a = Agent(http)
        sid = a.session("c-" + vid)["session"]
        first = a.pay(sid, "k-" + vid, drop=True)
        retry = a.pay(sid, retry_key(vid))
        back = a.read_back(sid)
        return vector(vid, rows, title, {"charges": charges(w, sid)},
                      [{"call": "complete", "rpc_id": a.ids[1], **first},
                       {"call": "complete (retry)", "rpc_id": a.ids[2], **retry},
                       {"call": "get_checkout_session", **back}],
                      {"from_answer": from_answer(retry), "after_read_back": from_read_back(back)},
                      {"charges": 1, "from_answer": answer, "after_read_back": "succeeded"}, note)


def in_process_vectors() -> list[dict]:
    out = [
        lost_complete("L1", "B02, R01; ryjen B", "complete: response lost, retry with the same key",
                      lambda v: "k-" + v, "succeeded"),
        lost_complete("L2", "ryjen A (fresh key)", "complete: response lost, retry with a new key",
                      lambda v: "k2-" + v, "failed",
                      "the session id is a server-issued operation identity: the retry is refused, not run "
                      "again; only a read-back shows it succeeded"),
        lost_complete("L3", "ryjen E (no key)", "complete: response lost, retry with no key",
                      lambda v: None, "failed", "as L2: no second charge, but the answer alone says failed"),
    ]

    # create has no server-issued identity: the key is all there is
    for vid, rows, title, keys, expected in (
            ("L4", "B01 shape, ryjen A", "create: response lost, retry with no key", (None, None), 2),
            ("L5", "B02, ryjen B", "create: response lost, retry with the same key", ("s5", "s5"), 1),
            ("L6", "ryjen D, T04", "create: the same cart again on purpose, with a new key", ("s6", "s6b"), 2)):
        with store() as (w, m, http, _):
            a = Agent(http)
            before = len(m.sessions)
            first = a.session(keys[0], drop=vid != "L6")
            second = a.session(keys[1])
            out.append(vector(vid, rows, title, {"sessions": len(m.sessions) - before},
                              [{"call": "create", **first}, {"call": "create (again)", **second}],
                              {"from_answer": "a session id" if "session" in second else second.get("error")},
                              {"sessions": expected},
                              "no money moves at create; an extra session is left open" if expected == 2 and
                              vid == "L4" else ""))

    with store() as (w, m, http, _):
        a = Agent(http)
        sid = a.session("c7")["session"]
        first = a.pay(sid, "k7", drop=True, request_id="req-1")
        retry = a.pay(sid, "k7", request_id="req-2")
        out.append(vector("L7", "harness requirement 2", "same key, a new meta.request_id on the retry",
                          {"charges": charges(w, sid)},
                          [{"call": "complete", **first}, {"call": "complete (retry)", **retry}],
                          {"from_answer": from_answer(retry)}, {"charges": 1, "from_answer": "succeeded"},
                          "the request digest leaves meta out, as REST leaves headers out"))

    with store() as (w, m, http, _):
        a = Agent(http)
        s1, s2 = a.session("c8a")["session"], a.session("c8b")["session"]
        first = a.pay(s1, "k8", drop=True)
        changed = a.pay(s2, "k8")
        out.append(vector("L8", "A06", "same key, different arguments (another session)",
                          {"charges": charges(w, s1) + charges(w, s2)},
                          [{"call": "complete", **first}, {"call": "complete (other session, same key)", **changed}],
                          {"from_answer": changed.get("error")}, {"charges": 1, "from_answer": "idempotency_conflict"},
                          "refused before anything runs; nothing from the first call is returned"))

    with store() as (w, m, http, _):
        a, b = Agent(http, "tok-a"), Agent(http, "tok-b")
        sa, sb = a.session("c9a")["session"], b.session("c9b")["session"]
        first = a.pay(sa, "shared", drop=True)
        other = b.pay(sb, "shared")
        leaked = other.get("session") == sa or other.get("order") == f"ord_{sa}"
        out.append(vector("L9", "A05", "another caller sends the same key for its own session",
                          {"charges_a": charges(w, sa), "charges_b": charges(w, sb), "a_receipt_leaked_to_b": leaked},
                          [{"caller": "A", "call": "complete", **first}, {"caller": "B", "call": "complete", **other}],
                          {"from_answer_b": from_answer(other)},
                          {"charges_a": 1, "charges_b": 1, "a_receipt_leaked_to_b": False, "from_answer_b": "succeeded"},
                          "keys are scoped to the authenticated caller and the tool"))

    out.append(in_flight())
    out.append(duplicate_delivery())
    out.append(restart())
    out.append(decline())
    return out


def in_flight() -> dict:
    """The rail is slow: the client times out while the charge is still out, and asks again."""
    with store() as (w, m, http, _):
        a = Agent(http)
        sid = a.session("c10")["session"]
        box = {}
        with held_rail(w) as release:
            paying = threading.Thread(target=lambda: box.setdefault("first", a.pay(sid, "k10", drop=True)))
            paying.start()
            wait_marked(m, sid)
            same, other, back = a.pay(sid, "k10"), a.pay(sid, "k10-new"), a.read_back(sid)
            release.set()
            paying.join()
        after = a.pay(sid, "k10")
        return vector("L10", "X02, ryjen C", "the rail is still answering when the client asks again",
                      {"charges": charges(w, sid)},
                      [{"call": "complete", **box["first"]}, {"call": "complete (same key, in flight)", **same},
                       {"call": "complete (new key, in flight)", **other},
                       {"call": "get_checkout_session (in flight)", **back},
                       {"call": "complete (same key, after)", **after}],
                      {"from_answer_in_flight": from_answer(same), "read_back_in_flight": from_read_back(back),
                       "from_answer_after": from_answer(after)},
                      {"charges": 1, "from_answer_in_flight": "unknown", "read_back_in_flight": "unknown",
                       "from_answer_after": "succeeded"},
                      "the read-back says complete_in_progress (ACP 2026-04-17), so a missing order is not "
                      "read as not paid")


def duplicate_delivery() -> dict:
    """The same request delivered 8 times at once: same JSON-RPC id, same key."""
    with store(psp_delay_s=0.2) as (w, m, http, _):
        a = Agent(http)
        before = len(m.sessions)
        made = []
        threads = [threading.Thread(target=lambda: made.append(a.session("c11"))) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        sid = made[0]["session"]
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(a.pay(sid, "k11", rpc_id=500))) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        tally = {}
        for x in answers:
            label = x.get("error") or x.get("status")
            tally[label] = tally.get(label, 0) + 1
        after = a.pay(sid, "k11")
        return vector("L11", "X01, C01", "8 identical creates, then 8 identical completes, delivered at once",
                      {"sessions": len(m.sessions) - before, "distinct_session_ids": len({x["session"] for x in made}),
                       "charges": charges(w, sid)},
                      [{"call": "complete x8 (answers)", **tally}, {"call": "complete (same key, after)", **after}],
                      {"from_answer_after": from_answer(after)},
                      {"sessions": 1, "distinct_session_ids": 1, "charges": 1, "from_answer_after": "succeeded"},
                      "one runs; the others wait on the key's lock and get its answer, or are told it is "
                      "in flight")


def restart() -> dict:
    """The answer is lost and the server restarts before the retry (a new process object, same database)."""
    with store() as (w, m, http, db):
        before = len(m.sessions)
        made = Agent(http).session("c12", drop=True)  # the client never learns the session id
        w2 = build(stores(), db=db)
        from fastapi.testclient import TestClient

        from .http import create_app
        a2 = Agent(TestClient(create_app(w2.merchants["northwind"], TOKENS)))
        again = a2.session("c12")
        sid = again["session"]
        first = a2.pay(sid, "k12", drop=True)
        w3 = build(stores(), db=db)
        a3 = Agent(TestClient(create_app(w3.merchants["northwind"], TOKENS)))
        retry = a3.pay(sid, "k12")
        return vector("L12", "durable recovery", "answers lost, server restarted before each retry",
                      {"sessions": len(w3.merchants["northwind"].sessions) - before, "charges": charges(w3, sid)},
                      [{"call": "create", **made}, {"call": "create (retry, new process)", **again},
                       {"call": "complete", **first}, {"call": "complete (retry, new process)", **retry}],
                      {"from_answer": from_answer(retry)}, {"sessions": 1, "charges": 1, "from_answer": "succeeded"},
                      "keys live in the store's database, not in the server process")


def decline() -> dict:
    """A declined card: the answer is lost; the retry with the same key gets the same decline, and a new
    attempt (new key, a good card) pays."""
    with store() as (w, m, http, _):
        a = Agent(http)
        sid = a.session("c13")["session"]
        first = a.pay(sid, "k13", card=BAD_CARD, drop=True)
        same = a.pay(sid, "k13", card=BAD_CARD)
        good = a.pay(sid, "k13-good")
        return vector("L13", "stored failure", "a decline is an answer too",
                      {"charges": charges(w, sid)},
                      [{"call": "complete (bad card)", **first}, {"call": "complete (retry, same key)", **same},
                       {"call": "complete (good card, new key)", **good}],
                      {"from_answer_retry": from_answer(same), "from_answer_good": from_answer(good)},
                      {"charges": 1, "from_answer_retry": "failed", "from_answer_good": "succeeded"},
                      "the decline is stored with the key once execution started; a new key is a new attempt")


# ---- the store in child processes: a process dies mid-payment ------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def server(db: str, crash: str = "", paying_timeout_s: float = 120.0, psp_delay_ms: int = 0):
    """``shipjack mcp-retry-vectors --serve`` in a child process; yields its base URL."""
    port = _free_port()
    env = {**os.environ, "SHIPJACK_RECONCILE_EVERY_S": "2"}  # the first retry lands before the first sweep
    proc = subprocess.Popen([sys.executable, "-m", "shipjack.retry_vectors", "serve", db, str(port), crash,
                             str(paying_timeout_s), str(psp_delay_ms)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("the store did not start")
        yield f"http://127.0.0.1:{port}", proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def serve(db: str, port: int, crash: str, paying_timeout_s: float, psp_delay_ms: int = 0) -> None:
    """The child process: the demo store with the vectors' tokens; with ``crash``, it ends itself
    (``os._exit``, no cleanup) right after the rail charged, or right before it would have."""
    import uvicorn

    from .http import demo_app
    app = demo_app(token="tok-a", db=db, psp_delay_ms=psp_delay_ms)
    merchant = app.state.world.merchants["northwind"]
    merchant.paying_timeout_s = paying_timeout_s
    real = merchant._charge

    def charge(attempt):
        if crash == "before_charge":
            os._exit(137)
        out = real(attempt)
        if crash == "after_charge":
            os._exit(137)
        return out

    merchant._charge = charge
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


def _wait_for(check, timeout_s: float = 15.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = check()
        if out is not None:
            return out
        time.sleep(0.2)
    return None


def crash_vectors() -> list[dict]:
    import httpx
    out = []
    for vid, rows, crash, title, timeout in (
            ("K1", "R03", "after_charge", "the process dies after the rail charged, before the order", 120.0),
            ("K2", "R02, then authoritative non-execution", "before_charge",
             "the process dies after marking the attempt, before the rail call", 1.5)):
        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "store.db")
            with server(db, crash, timeout) as (url, proc), httpx.Client(base_url=url, timeout=10) as http:
                a = Agent(http)
                sid = a.session("c-" + vid)["session"]
                first = a.pay(sid, "k-" + vid)
                proc.wait(timeout=10)
                died = proc.returncode
            with server(db, "", timeout) as (url, _), httpx.Client(base_url=url, timeout=10) as http:
                a = Agent(http)
                at_once = a.pay(sid, "k-" + vid)
                back_at_once = a.read_back(sid)
                settled = _wait_for(lambda: (lambda r: r if from_answer(r) != "unknown" else None)(a.pay(sid, "k-" + vid)))
                back = a.read_back(sid)
            w = build(stores(), db=db)
            out.append(vector(vid, rows, title, {"charges": charges(w, sid)},
                              [{"call": "complete", **first, "server_exit_code": died},
                               {"call": "complete (same key, new process, at once)", **at_once},
                               {"call": "get_checkout_session (at once)", **back_at_once},
                               {"call": "complete (same key, once settled)", **(settled or {"error": "never settled"})},
                               {"call": "get_checkout_session (after)", **back}],
                              {"at_once": from_answer(at_once), "read_back_at_once": from_read_back(back_at_once),
                               "once_settled": from_answer(settled or {}), "after_read_back": from_read_back(back)},
                              {"charges": 1, "at_once": ("unknown", "succeeded"),
                               "read_back_at_once": ("unknown", "succeeded"), "once_settled": "succeeded",
                               "after_read_back": "succeeded"},
                              "reconciliation finds the charge in the ledger by the payment's key and records the "
                              "order; the answer is stored under the client's key, so its retry gets the receipt"
                              if vid == "K1" else
                              "no charge carries the payment's key; once the attempt is stale (1.5 s here, 120 s "
                              "by default) reconciliation frees the session and the client's key, and the retry "
                              "pays once. With Stripe it would first resend the same payment key"))

    with tempfile.TemporaryDirectory() as folder:
        db = os.path.join(folder, "store.db")
        build(stores(), db=db)  # the schema, before three processes race to make it
        # a 300 ms rail, so the completes overlap the charge instead of queueing behind it
        with server(db, psp_delay_ms=300) as (u1, _), server(db, psp_delay_ms=300) as (u2, _), \
                server(db, psp_delay_ms=300) as (u3, _):
            clients = [httpx.Client(base_url=u, timeout=20) for u in (u1, u2, u3) for _ in range(3)]
            made, answers = [], []
            threads = [threading.Thread(target=lambda c=c: made.append(Agent(c).session("c-K3"))) for c in clients]
            [t.start() for t in threads]
            [t.join() for t in threads]
            sid = made[0]["session"]
            threads = [threading.Thread(target=lambda c=c: answers.append(Agent(c).pay(sid, "k-K3"))) for c in clients]
            [t.start() for t in threads]
            [t.join() for t in threads]
            after = Agent(clients[-1]).pay(sid, "k-K3")
            for c in clients:
                c.close()
        tally = {}
        for x in answers:
            label = x.get("error") or x.get("status")
            tally[label] = tally.get(label, 0) + 1
        w = build(stores(), db=db)
        out.append(vector("K3", "C01, X01 across processes", "9 identical creates, then 9 identical completes, "
                          "spread over 3 server processes on one database",
                          {"distinct_session_ids": len({x.get("session") for x in made}), "charges": charges(w, sid)},
                          [{"call": "complete x9 (answers)", **tally}, {"call": "complete (same key, after)", **after}],
                          {"from_answer_after": from_answer(after)},
                          {"distinct_session_ids": 1, "charges": 1, "from_answer_after": "succeeded"},
                          "one runs; while it is out, the others are told it is in flight"))
    return out


# ---- the report -------------------------------------------------------------------------------------------


def build_vectors(crashes: bool = True) -> dict:
    vectors = in_process_vectors() + (crash_vectors() if crashes else [])
    try:
        mine = version("shipjack")
    except PackageNotFoundError:
        mine = "unknown"
    return {"what": "lost-response retry vectors for shipjack's MCP checkout tools (MCP issue #3394)",
            "transport": "MCP Streamable HTTP (JSON answers), protocol 2025-11-25; ACP MCP binding, API "
                         + ACP_API_VERSION,
            "store": "shipjack " + mine + ", SQLite (WAL), simulated payment processor",
            "fault": "in-process: the client drops the answer after the server committed; K: the server process "
                     "ends itself with os._exit and a new one starts on the same database",
            "all_as_expected": all(v["as_expected"] for v in vectors), "vectors": vectors}


def table(report: dict) -> str:
    rows = ["| ID | Matrix | Case | Effects | Reported | As expected |", "|---|---|---|---|---|---|"]
    for v in report["vectors"]:
        effects = ", ".join(f"{k} {v_}" for k, v_ in v["effects"].items())
        reported = ", ".join(f"{k}: {v_}" for k, v_ in v["reported"].items())
        rows.append(f"| {v['id']} | {v['matrix']} | {v['title']} | {effects} | {reported} | "
                    f"{'yes' if v['as_expected'] else 'NO'} |")
    return "\n".join(rows)


def add_parser(sub) -> None:
    p = sub.add_parser("mcp-retry-vectors", help="lost-response retry vectors for the MCP checkout tools (MCP #3394)")
    p.add_argument("--json", help="also write the report here")
    p.add_argument("--no-crash", action="store_true", help="skip the vectors that kill a server process")
    p.set_defaults(func=main)


def main(args) -> int:
    report = build_vectors(crashes=not args.no_crash)
    print(table(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
            f.write("\n")
    return 0 if report["all_as_expected"] else 1


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "serve":
    serve(sys.argv[2], int(sys.argv[3]), sys.argv[4], float(sys.argv[5]), int(sys.argv[6]))
