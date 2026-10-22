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


def test_an_order_and_its_webhook_commit_together_and_a_landed_charge_is_never_made_twice(target):
    """The rail call runs outside the transaction, so once it returns the money has moved. A failure
    while recording the order rolls back the order and its webhook together; the next attempt finds the
    charge by its idempotency key and records the order from it, without charging again."""
    w, m = shop(target)
    box = Outbox("https://platform.example/hook", SECRET, send=lambda *a: 200).attach(m)
    sid = new_session(m)

    def broken(event, order):
        raise RuntimeError("listener failed")

    m.listeners.append(broken)
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD)
    assert not m.orders and box.pending() == 0 and len(w.processor.charges) == 1
    assert m.sessions[sid].paying is not None  # the attempt is still marked
    m.listeners.remove(broken)
    assert m.complete_session(sid, CARD)["status"] == "completed"
    assert len(w.processor.charges) == 1 and len(m.orders) == 1 and box.pending() == 1


def test_a_crash_after_stripe_charged_does_not_charge_twice(target, monkeypatch):
    """The rail charged and the ledger recorded it, then the process died before it recorded the order.
    The retry finds the charge by its key and records the order; Stripe is not called again."""
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
    assert not m.orders and m.get_session(sid)["status"] == "complete_in_progress"  # still marked
    out = m.complete_session(sid, CARD)
    assert out["status"] == "completed"
    assert len(stripe._calls["intents"]) == 1 and len(stripe._calls["keys"]) == 1
    assert m.dispute_evidence(out["order"]["id"])["charge"]["psp_ref"] == "pi_fake_1"


def test_a_crash_before_the_charge_was_recorded_asks_stripe_again_with_the_same_key(target, monkeypatch):
    """Worst case: Stripe charged, then the process died before anything was recorded. Until the attempt
    is stale a retry is told it is in progress; then reconciliation sends the same request with the
    same idempotency key, Stripe answers with the first PaymentIntent, and the order is recorded once."""
    stripe = idempotent_stripe()
    monkeypatch.setitem(sys.modules, "stripe", stripe)
    world = build(stores(), processor=StripeProcessor(api_key="sk_test_x"), db=target)
    m = world.merchants["northwind"]
    sid = new_session(m)
    ledger, crashes = world.processor.charges, iter([True])

    class DiesOnce:  # the ledger write is where the process dies
        def append(self, charge):
            if next(crashes, False):
                raise RuntimeError("the process died here")
            ledger.append(charge)

        def __getattr__(self, name):
            return getattr(ledger, name)

    world.processor.charges = DiesOnce()
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD)
    with pytest.raises(CheckoutError) as busy:
        m.complete_session(sid, CARD)
    assert busy.value.code == "payment_in_progress" and len(stripe._calls["intents"]) == 1
    assert m.reconcile_payments(stale_after_s=0) == {"finished": 1, "freed": 0, "running": 0}
    assert m.get_session(sid)["status"] == "completed" and len(m.orders) == 1
    assert len(stripe._calls["intents"]) == 1 and stripe._calls["keys"][0] == stripe._calls["keys"][1]


def test_a_crash_before_the_rail_answered_frees_the_session_once_stale(target, monkeypatch):
    w, m = shop(target)
    sid = new_session(m)
    real, crashes = w.processor._settle, iter([True])

    def settle(*args, **kwargs):
        if next(crashes, False):
            raise RuntimeError("the process died here")
        return real(*args, **kwargs)

    monkeypatch.setattr(w.processor, "_settle", settle)
    with pytest.raises(RuntimeError):
        m.complete_session(sid, CARD)
    assert m.reconcile_payments() == {"finished": 0, "freed": 0, "running": 1}  # not stale yet
    assert m.reconcile_payments(stale_after_s=0) == {"finished": 0, "freed": 1, "running": 0}
    assert m.complete_session(sid, CARD)["status"] == "completed"
    assert len(w.processor.charges) == 1 and m.sessions[sid].attempts == 1  # a new key for the new attempt


def test_a_session_cannot_change_while_its_payment_is_in_flight(target):
    w, m = shop(target)
    w.processor.settle_delay_s = 0.6
    sid = new_session(m)
    paying = threading.Thread(target=m.complete_session, args=(sid, CARD))
    paying.start()
    time.sleep(0.25)
    for change in (lambda: m.update_session(sid, items=[{"id": HUB.id, "quantity": 3}]),
                   lambda: m.cancel_session(sid), lambda: m.ap2_checkout(sid),
                   lambda: m.complete_session(sid, CARD)):
        with pytest.raises(CheckoutError) as refused:
            change()
        assert refused.value.code == "payment_in_progress"
    paying.join()
    assert m.get_session(sid)["status"] == "completed" and len(w.processor.charges) == 1


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
    # one charge and one order, whoever records it: a racer that finds the charge already on record
    # finishes the order from it and answers "completed" too; the rest are told it is paid or in progress
    assert len(w.processor.charges) == 1 and len(m.orders) == 1, results
    assert "completed" in results and set(results) <= {"completed", "invalid", "payment_in_progress"}, results


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


# ---- per-session locks (Postgres) ------------------------------------------------------------------------

pg_only = pytest.mark.skipif(not PG, reason="set SHIPJACK_TEST_PG to a Postgres URL")


@pytest.fixture
def pg_db(request):
    url, schema = pg_target()
    request.getfixturevalue("pg_cleanup").append(schema)
    return url


def test_a_slow_payment_on_one_session_does_not_hold_up_another(target):
    """The rail call runs outside the transaction, so even SQLite's single writer is free meanwhile."""
    w, m = shop(target)
    w.processor.settle_delay_s = 0.5  # like a real payment API
    sids = [new_session(m) for _ in range(2)]
    out = {}

    def pay(sid):
        out[sid] = m.complete_session(sid, CARD)["status"]

    threads = [threading.Thread(target=pay, args=(s,)) for s in sids]
    began = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - began
    assert sorted(out.values()) == ["completed", "completed"] and len(w.processor.charges) == 2
    assert elapsed < 0.9, f"{elapsed:.2f} s: two different shoppers' payments waited for each other"


@pg_only
def test_a_keyed_change_cannot_rewrite_a_session_it_did_not_lock(pg_db):
    _, m = shop(pg_db)
    a, b = new_session(m), new_session(m)
    with pytest.raises(RuntimeError, match="without holding its lock"), m.repo.transaction(keys=(m.repo.session_key(a),)):
        m.repo.session(b).status = "canceled"
    assert m.get_session(b)["status"] == "ready_for_payment"  # nothing was written


@pg_only
def test_a_store_wide_change_still_waits_for_keyed_ones(pg_db):
    """replace_catalog (no keys) must not run while any keyed transaction is open."""
    _, m = shop(pg_db)
    sid = new_session(m)
    inside = threading.Event()

    def hold():
        with m.repo.transaction(keys=(m.repo.session_key(sid),)):
            inside.set()
            time.sleep(0.6)

    holder = threading.Thread(target=hold)
    holder.start()
    inside.wait()
    began = time.perf_counter()
    m.replace_catalog(list(m.products.values()))
    waited = time.perf_counter() - began
    holder.join()
    assert waited > 0.3, f"the catalog swap ran during a keyed transaction ({waited:.2f} s)"


def test_a_catalog_change_during_a_payment_does_not_change_what_the_order_records(target):
    """No lock is held while the rail answers, so the catalog may change then. The order records what
    was priced and charged when the attempt began."""
    from shipjack.catalog import Product
    w, m = shop(target)
    w.processor.settle_delay_s = 0.5
    sid = new_session(m)
    paying = threading.Thread(target=m.complete_session, args=(sid, CARD))
    paying.start()
    time.sleep(0.2)
    m.replace_catalog([Product("nw-cable-2m", "Northwind USB-C Cable 2m", 1299)])  # the hub is gone
    paying.join()
    order = m.orders[0]
    assert order["amount"] == 3499 and order["lines"][0]["unit_price"] == 3499
    assert order["lines"][0]["product_id"] == HUB.id and len(w.processor.charges) == 1


def test_charges_and_refunds_at_once_never_wait_on_each_other_forever(target):
    """A charge writes the ledger outside any transaction while a refund runs inside one. If the
    processor held its own lock during that ledger write, the two would wait on each other (SQLite has
    one writer). Everything must finish, each charge and refund exactly once."""
    w, m = shop(target)
    paid = [m.complete_session(new_session(m), CARD)["order"]["id"] for _ in range(6)]
    fresh = [new_session(m) for _ in range(6)]
    errors = []

    def run(fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(repr(exc))

    threads = ([threading.Thread(target=run, args=(m.complete_session, sid, CARD)) for sid in fresh]
               + [threading.Thread(target=run, args=(m.refund_order, oid)) for oid in paid])
    began = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "deadlocked"
    assert not errors and time.perf_counter() - began < 20, errors
    assert len(w.processor.charges) == 12 and len(w.processor.refunds) == 6


def test_over_http_a_second_request_waits_its_turn_and_the_charge_happens_once(target):
    from shipjack.retry_vectors import held_rail, wait_marked
    w, m = shop(target)
    client = TestClient(create_app(m, {"t": "platform"}))
    head = {"Authorization": "Bearer t", "API-Version": ACP_API_VERSION}
    sid = client.post("/checkout_sessions", json={"items": [{"id": HUB.id}], "fulfillment_details": {"address": HOME}},
                      headers={**head, "Idempotency-Key": "c1"}).json()["id"]
    path = f"/checkout_sessions/{sid}/complete"
    first = {}
    with held_rail(w) as release:  # the charge stays out until every retry has been answered
        paying = threading.Thread(target=lambda: first.setdefault(
            "r", client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k1"})))
        paying.start()
        wait_marked(m, sid)
        same_key = client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k1"})
        again = client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k1"})
        other_key = client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k2"})
        release.set()
        paying.join()
    assert again.json()["code"] == "idempotency_in_flight"  # a refused retry leaves the first request's key
    later = client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k2"})
    assert later.status_code == 400 and later.json()["code"] == "invalid"  # "in progress" was not stored for k2
    assert same_key.status_code == 409 and same_key.json()["code"] == "idempotency_in_flight"  # ACP 2026-04-17
    assert other_key.status_code == 409 and other_key.json()["code"] == "payment_in_progress"
    assert same_key.headers["Retry-After"] == other_key.headers["Retry-After"] == "1"
    assert first["r"].status_code == 200 and first["r"].json()["status"] == "completed"
    replay = client.post(path, json={"payment_data": CARD}, headers={**head, "Idempotency-Key": "k1"})
    assert replay.headers.get("Idempotent-Replayed") == "true" and replay.json() == first["r"].json()
    assert len(w.processor.charges) == 1


def test_the_processor_never_holds_its_lock_while_writing_the_ledger(target):
    """The deterministic form of the deadlock above: whenever a charge is written to the ledger (a
    database write, with its own lock), another thread must be able to take the processor's lock."""
    w, m = shop(target)
    ledger, held = w.processor.charges, []

    class Probe:
        def append(self, charge):
            got = []

            def try_lock():
                ok = w.processor._lock.acquire(timeout=0.5)
                got.append(ok)
                if ok:
                    w.processor._lock.release()

            other = threading.Thread(target=try_lock)
            other.start()
            other.join()
            held.append(not got[0])
            ledger.append(charge)

        def __getattr__(self, name):
            return getattr(ledger, name)

        def __len__(self):
            return len(ledger)

    w.processor.charges = Probe()
    m.complete_session(new_session(m), CARD)
    assert held == [False], "the processor lock was held during the ledger write"


def test_no_transaction_is_open_while_the_payment_rail_answers(target):
    """The point of the three-step payment: whether called directly or over HTTP, the thread that calls
    the payment rail holds no database transaction (and so no lock) while it waits for the answer."""
    w, m = shop(target)
    inside, real = [], w.processor._settle

    def settle(*args, **kwargs):
        inside.append(m.repo.db.active)
        return real(*args, **kwargs)

    w.processor._settle = settle
    m.complete_session(new_session(m), CARD)
    client = TestClient(create_app(m, {"t": "platform"}))
    head = {"Authorization": "Bearer t", "API-Version": ACP_API_VERSION}
    sid = client.post("/checkout_sessions", json={"items": [{"id": HUB.id}], "fulfillment_details": {"address": HOME}},
                      headers={**head, "Idempotency-Key": "c"}).json()["id"]
    paid = client.post(f"/checkout_sessions/{sid}/complete", json={"payment_data": CARD},
                       headers={**head, "Idempotency-Key": "p"})
    assert paid.json()["status"] == "completed"
    assert inside == [False, False], "a transaction was open during the rail call"
