"""The lost-response retry vectors (MCP #3394) hold, and the pieces under them are tested directly."""

import pytest
import threading

from conftest import HOME, HUB

from shipjack.idempotency import InFlight, Key, Replay
from shipjack.retry_vectors import Agent, crash_vectors, held_rail, in_process_vectors, store, wait_marked

CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}


def failing(vectors):
    return {v["id"]: {"effects": v["effects"], "reported": v["reported"], "expected": v["expected"]}
            for v in vectors if not v["as_expected"]}


def test_every_in_process_vector_holds():
    vectors = in_process_vectors()
    assert len(vectors) == 13 and failing(vectors) == {}


def test_every_vector_that_kills_a_server_process_holds():
    pytest.importorskip("uvicorn")
    vectors = crash_vectors()
    assert [v["id"] for v in vectors] == ["K1", "K2", "K3"] and failing(vectors) == {}


def test_a_key_is_answered_by_whoever_finishes_the_payment(world):
    """The request that marked the payment dies after the rail charged. Reconciliation records the
    order and stores the answer under that request's key; nothing in the request's own code runs."""
    m = world.merchants["northwind"]
    sid = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})["id"]
    key = Key(m.repo, m.id, ("platform", "mcp:complete_checkout_session", "k1"), "fp")
    real = m.repo.add_order

    def dies(order, record):
        raise RuntimeError("the process died here")

    m.repo.add_order = dies
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD, record=key)
    m.repo.add_order = real
    with m.repo.transaction(keys=key.keys), pytest.raises(InFlight):
        key.start()  # the dead request still holds its claim
    assert m.reconcile_payments() == {"finished": 1, "freed": 0, "running": 0}
    with m.repo.transaction(keys=key.keys):
        answer = key.start()
    assert isinstance(answer, Replay) and answer.code == 200
    assert answer.payload["status"] == "completed" and answer.payload["order"]["id"] == f"ord_{sid}"
    assert len(world.processor.charges) == 1


def test_a_freed_attempt_frees_its_key(world):
    """No charge carries the attempt's payment key and the attempt is stale: the session is free again,
    and so is the client's key, so its retry runs and pays once."""
    m = world.merchants["northwind"]
    sid = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})["id"]
    key = Key(m.repo, m.id, ("platform", "/checkout_sessions/x/complete", "k1"), "fp")
    real = world.processor._settle

    def dies(*a, **kw):
        raise RuntimeError("the process died here")

    world.processor._settle = dies
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD, record=key)
    world.processor._settle = real
    assert m.get_session(sid)["status"] == "complete_in_progress"
    assert m.reconcile_payments(stale_after_s=0) == {"finished": 0, "freed": 1, "running": 0}
    assert m.repo.idempotent(key.slot) is None and m.get_session(sid)["status"] == "ready_for_payment"
    assert m.complete_session(sid, CARD, record=key)["status"] == "completed"
    assert len(world.processor.charges) == 1


def test_a_retry_told_the_key_is_in_flight_leaves_the_key_alone():
    """Each same-key retry while the first request is out gets idempotency_in_flight: a refused retry
    must not free the key the first request holds. A new key gets payment_in_progress, which is not an
    answer to keep: later, that key runs (and is told the session is already completed)."""
    with store() as (w, m, http, _), held_rail(w) as release:
        a = Agent(http)
        sid = a.session("c")["session"]
        paying = threading.Thread(target=lambda: a.pay(sid, "k", drop=True))
        paying.start()
        wait_marked(m, sid)
        answers = [a.pay(sid, "k") for _ in range(3)]
        other = a.pay(sid, "k-new")
        release.set()
        paying.join()
        assert [x.get("error") for x in answers] == ["idempotency_in_flight"] * 3
        assert other.get("error") == "payment_in_progress"
        assert a.pay(sid, "k")["status"] == "completed"
        assert a.pay(sid, "k-new")["error"] == "invalid"  # "in progress" was not stored as k-new's answer


def test_one_key_sent_at_once_from_many_threads_makes_one_session(world):
    """On Postgres two creates share no session lock, so only the key's own lock stops a duplicate."""
    from shipjack.mcp import MCPStore
    from shipjack.merchant import ACP_API_VERSION
    m = world.merchants["northwind"]
    tools = MCPStore(m, caller="platform")
    args = {"meta": {"api_version": ACP_API_VERSION, "idempotency_key": "same"},
            "payload": {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": HOME}}}
    before, made = len(m.sessions), []
    start = threading.Barrier(8)

    def create():
        start.wait()
        made.append(tools.call_tool("create_checkout_session", args)["id"])

    threads = [threading.Thread(target=create) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(made)) == 1 and len(m.sessions) - before == 1


def test_a_payment_over_mcp_does_not_hold_up_other_requests(tmp_path):
    """The MCP endpoint's work runs in the thread pool: while one payment waits on the rail, the same
    server process still answers everyone else."""
    import socket

    import httpx
    uvicorn = pytest.importorskip("uvicorn")
    from shipjack.http import demo_app
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = demo_app(token="tok-a", db=str(tmp_path / "s.db"))
    world, m = app.state.world, app.state.world.merchants["northwind"]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        while not server.started:
            threading.Event().wait(0.05)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as http, held_rail(world) as release:
            a = Agent(http)
            sid = a.session("c")["session"]
            paying = threading.Thread(target=lambda: a.pay(sid, "k"))
            paying.start()
            wait_marked(m, sid)
            assert http.get("/.well-known/acp.json", timeout=2).status_code == 200  # not stuck behind it
            assert a.read_back(sid)["status"] == "complete_in_progress"
            release.set()
            paying.join()
    finally:
        server.should_exit = True
