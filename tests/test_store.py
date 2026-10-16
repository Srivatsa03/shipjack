"""The SQLite store: state survives a restart, a failed change leaves nothing behind, a retry after a crash
pays once, and several OS processes sharing one file never charge, refund or number anything twice.

Every other test that takes a ``world`` already runs against both stores (see conftest.py). These are
the properties only a database can have, on SQLite and (with SHIPJACK_TEST_PG) Postgres.
"""

import json
import multiprocessing
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import pytest
from conftest import HOME, HUB, PG, hub_intent, pg_target, stores
from fastapi.testclient import TestClient
from jwcrypto.jws import JWS
from test_lifecycle import fake_stripe as idempotent_stripe
from test_stripe import fake_stripe as declining_stripe

from shipjack.http import create_app
from shipjack.merchant import ACP_API_VERSION, CheckoutError
from shipjack.psp import PaymentError
from shipjack.stripe_psp import TEST_PM_DECLINE, TEST_PM_OK, StripeProcessor
from shipjack.webhooks import RECEIVER_PATH, Outbox, create_receiver
from shipjack.world import build

CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}
SECRET = b"s" * 32


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG else []))
def target(request, tmp_path):
    """Where the store lives: a SQLite file, or (with SHIPJACK_TEST_PG) a fresh Postgres schema."""
    if request.param == "sqlite":
        return str(tmp_path / "s.db")
    url, schema = pg_target()
    request.getfixturevalue("pg_cleanup").append(schema)
    return url


def shop(db):
    world = build(stores(), db=str(db))
    return world, world.merchants["northwind"]


def new_session(m):
    return m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})["id"]


# ---- a restart -------------------------------------------------------------------------------------------


def test_everything_survives_a_restart(target):
    _, m1 = shop(target)
    sid = new_session(m1)
    oid = m1.complete_session(sid, CARD)["order"]["id"]
    m1.refund_order(oid, 500)
    m1.fulfill_order(oid, carrier="UPS", tracking_number="1Z999")
    before = m1.acp_order(oid)

    w2, m2 = shop(target)  # a new process on the same file
    assert m2.get_session(sid)["status"] == "completed"
    assert m2.acp_order(oid) == before
    assert [e["event"] for e in m2.audit] == ["session_created", "order_paid", "order_refunded", "order_fulfilled"]
    assert len(w2.processor.charges) == 1 and w2.processor.charges[0].refunded == 500
    assert m2.key.thumbprint() == m1.key.thumbprint()
    assert new_session(m2) == "cs_northwind_2"  # ids continue, never reused
    assert m2.refund_order(oid)["remaining"] == 0  # the rest can still be refunded, and no more
    with pytest.raises(PaymentError):
        m2.refund_order(oid, 1)


def test_a_checkout_signed_before_a_restart_verifies_and_pays_after_it(target):
    _, m1 = shop(target)
    sid = new_session(m1)
    signed = m1.ap2_checkout(sid)["checkout_jwt"]
    w2, m2 = shop(target)
    jws = JWS()
    jws.deserialize(signed)
    jws.verify(m2.verification_key())  # the restarted store publishes the key that signed it
    wallet = w2.wallet("ap2", hub_intent(w2))
    assert wallet.pay(m2, sid)["status"] == "paid"


def test_an_idempotent_replay_survives_a_restart(target):
    headers = {"Authorization": "Bearer t", "API-Version": ACP_API_VERSION, "Idempotency-Key": "k-1"}
    body = {"items": [{"id": HUB.id}], "fulfillment_details": {"address": HOME}}
    first = TestClient(create_app(shop(target)[1], {"t": "platform"})).post(
        "/checkout_sessions", json=body, headers=headers)
    again = TestClient(create_app(shop(target)[1], {"t": "platform"})).post(
        "/checkout_sessions", json=body, headers=headers)
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()


# ---- all or nothing ----------------------------------------------------------------------------------------


def test_an_order_its_charge_and_its_webhook_commit_together_or_not_at_all(target):
    w, m = shop(target)
    box = Outbox("https://platform.example/hook", SECRET, send=lambda *a: 200).attach(m)
    sid = new_session(m)

    def broken(event, order):
        raise RuntimeError("listener failed")

    m.listeners.append(broken)
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD)
    assert not m.orders and not w.processor.charges and box.pending() == 0
    assert m.get_session(sid)["status"] == "ready_for_payment"
    m.listeners.remove(broken)
    assert m.complete_session(sid, CARD)["status"] == "completed"
    assert len(w.processor.charges) == 1 and box.pending() == 1


def test_a_crash_after_stripe_charged_does_not_charge_twice(target, monkeypatch):
    """The rail charged, then the process died before it recorded the order. The retry sends the same
    idempotency key, so Stripe returns the first PaymentIntent instead of charging again."""
    stripe = idempotent_stripe()
    monkeypatch.setitem(sys.modules, "stripe", stripe)
    world = build(stores(), processor=StripeProcessor(api_key="sk_test_x"), db=target)
    m = world.merchants["northwind"]
    sid = new_session(m)
    real, crashes = m.repo.add_order, iter([True])

    def add_order(order, record):
        if next(crashes, False):
            raise RuntimeError("the process died here")
        return real(order, record)

    monkeypatch.setattr(m.repo, "add_order", add_order)
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD)
    assert not m.orders and m.get_session(sid)["status"] == "ready_for_payment"
    out = m.complete_session(sid, CARD)
    assert out["status"] == "completed"
    assert len(stripe._calls["intents"]) == 1 and stripe._calls["keys"][0] == stripe._calls["keys"][1]
    assert m.dispute_evidence(out["order"]["id"])["charge"]["psp_ref"] == "pi_fake_1"


def test_a_crash_after_stripe_refunded_does_not_refund_twice(target, monkeypatch):
    stripe = idempotent_stripe()
    monkeypatch.setitem(sys.modules, "stripe", stripe)
    world = build(stores(), processor=StripeProcessor(api_key="sk_test_x"), db=target)
    m = world.merchants["northwind"]
    oid = m.complete_session(new_session(m), CARD)["order"]["id"]
    crashes = iter([True])

    def dies_once(event, order):
        if event == "order_update" and next(crashes, False):
            raise RuntimeError("the process died here")

    m.listeners.append(dies_once)
    with pytest.raises(RuntimeError):
        m.refund_order(oid, 1000)
    assert m.acp_order(oid)["adjustments"] == [] and world.processor.charges[0].refunded == 0
    assert m.refund_order(oid, 1000)["psp_ref"] == "re_fake_1"
    assert len(stripe._calls["refunds"]) == 1 and world.processor.charges[0].refunded == 1000


def test_a_declined_attempt_gets_a_new_key_so_the_retry_can_succeed(target, monkeypatch):
    stripe = declining_stripe()
    monkeypatch.setitem(sys.modules, "stripe", stripe)
    processor = StripeProcessor(api_key="sk_test_x", payment_method=TEST_PM_DECLINE)
    world = build(stores(), processor=processor, db=target)
    m = world.merchants["northwind"]
    sid = new_session(m)
    assert m.complete_session(sid, CARD)["error"]["code"] == "payment_declined"
    processor.payment_method = TEST_PM_OK
    assert m.complete_session(sid, CARD)["status"] == "completed"
    assert len(set(stripe._keys)) == 2


# ---- several processes, one file ----------------------------------------------------------------------------

SPAWN = multiprocessing.get_context("spawn")


def _at(start: float) -> None:
    time.sleep(max(0.0, start - time.time()))


def _pay(db: str, sid: str, start: float) -> str:
    _, m = shop(db)
    _at(start)
    try:
        out = m.complete_session(sid, CARD)
    except CheckoutError as exc:  # the session was already paid by another process
        return exc.code
    return out["status"] if "error" not in out else out["error"]["code"]


def _refund(db: str, oid: str, start: float) -> str:
    _, m = shop(db)
    _at(start)
    try:
        return str(m.refund_order(oid)["refunded"])
    except PaymentError as exc:
        return exc.code


def _sessions(db: str, n: int, start: float) -> list[str]:
    _, m = shop(db)
    _at(start)
    return [new_session(m) for _ in range(n)]


def _shop_and_send(db: str, url: str, start: float) -> int:
    _, m = shop(db)
    box = Outbox(url, SECRET).attach(m)
    _at(start)
    for _ in range(5):
        oid = m.complete_session(new_session(m), CARD)["order"]["id"]
        m.fulfill_order(oid)
        m.mark_delivered(oid)
        box.run_once()
    deadline = time.time() + 20
    while box.pending() and time.time() < deadline:
        box.run_once()
        time.sleep(0.05)
    box.stop()
    return len(box.delivered)


def race(fn, args, n):
    start = time.time() + 4  # every process builds its store first, then they all go at once
    with ProcessPoolExecutor(n, mp_context=SPAWN) as pool:
        return [f.result() for f in [pool.submit(fn, *args, start) for _ in range(n)]]


def test_refusals_cross_a_process_boundary():
    import pickle
    for exc in (CheckoutError("invalid", "session is already completed", "$.status"),
                PaymentError("payment_declined", "no")):
        back = pickle.loads(pickle.dumps(exc))
        assert type(back) is type(exc) and back.code == exc.code and str(back) == str(exc)


def test_processes_racing_to_pay_one_session_charge_it_once(target):
    db = target
    w, m = shop(db)
    sid = new_session(m)
    results = race(_pay, (db, sid), 8)
    assert sorted(results).count("completed") == 1, results
    assert len(w.processor.charges) == 1 and len(m.orders) == 1


def test_processes_racing_to_refund_one_order_refund_it_once(target):
    db = target
    w, m = shop(db)
    oid = m.complete_session(new_session(m), CARD)["order"]["id"]
    results = race(_refund, (db, oid), 8)
    assert results.count("3499") == 1, results
    assert w.processor.charges[0].refunded == 3499 and len(w.processor.refunds) == 1


def test_processes_creating_sessions_at_once_never_share_an_id(target):
    db = target
    shop(db)
    ids = [i for batch in race(_sessions, (db, 25), 4) for i in batch]
    assert len(ids) == 100 and len(set(ids)) == 100


@pytest.fixture
def live_receiver():
    uvicorn = pytest.importorskip("uvicorn")
    from test_webhooks import free_port
    app = create_receiver(SECRET)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}{RECEIVER_PATH}", app.state.receiver
    server.should_exit = True
    thread.join(timeout=5)


def test_processes_share_one_outbox_and_keep_each_orders_events_in_order(target, live_receiver):
    url, state = live_receiver
    db = target
    shop(db)
    delivered = race(_shop_and_send, (db, url), 4)
    assert sum(delivered) == 60 and state.duplicates == 0
    by_order = {}
    for event in state.events:
        by_order.setdefault(event["data"]["id"], []).append(event["data"]["status"])
    assert len(by_order) == 20 and all(s == ["created", "shipped", "completed"] for s in by_order.values())


def test_a_dead_senders_lease_expires_and_another_process_takes_over(target):
    _, m = shop(target)
    clock = type("C", (), {"t": 1_800_000_000.0, "__call__": lambda self: self.t})()
    sent = []
    first = Outbox("https://platform.example/hook", SECRET, send=lambda u, b, h, t: sent.append("first") or 200,
                   clock=clock, lease_ttl=30).attach(m)
    second = Outbox("https://platform.example/hook", SECRET, send=lambda u, b, h, t: sent.append("second") or 200,
                    clock=clock, lease_ttl=30)
    second.queue = first.queue.__class__(first.queue.db, first.queue.name)  # the same queue, another process
    m.complete_session(new_session(m), CARD)
    assert first.queue.acquire(first._holder, clock(), 30)  # the first sender holds the lease, then dies
    assert second.run_once() == 0 and sent == []
    clock.t += 31
    assert second.run_once() == 1 and sent == ["second"]
    assert json.loads(second.delivered[0].body)["type"] == "order_create"
