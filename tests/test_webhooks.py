"""ACP order webhooks: signing, the order snapshots they carry, delivery with retries, and the receiver.

The rules: every event is the full ACP Order and validates against the spec's schema; a receiver
accepts only a fresh, correct signature; delivery is retried on transient failures, never retried on a
bad event, and one order's events always arrive in the order they happened; a replayed or older
snapshot never changes what the receiver believes.
"""

import hashlib
import hmac
import json
import pathlib
import socket
import threading
import time

import pytest
from conftest import EMAIL, GIFT, HOME, HUB
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from shipjack import webhooks
from shipjack.http import demo_app
from shipjack.webhooks import (
    RECEIVER_PATH,
    Outbox,
    SignatureError,
    create_receiver,
    secret_from_env,
    sign,
    verify,
)

SECRET = b"s" * 32
URL = "https://platform.example/agentic_checkout/webhooks/order_events"
CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}
SCHEMA = Draft202012Validator(json.loads(
    (pathlib.Path(__file__).parent / "spec" / "acp-2026-04-17-order-webhook.schema.json").read_text()))


def schema_errors(event: dict) -> list[str]:
    return [f"{list(e.path)}: {e.message}" for e in SCHEMA.iter_errors(event)]


def buy(world, items=((HUB.id, 1),), details=None):
    m = world.merchants["northwind"]
    s = m.create_session([{"id": p, "quantity": q} for p, q in items],
                         fulfillment_details=details or {"address": HOME})
    return m, m.complete_session(s["id"], CARD)["order"]["id"]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Platform:
    """A fake receiver for the outbox: answers from a script, records what it got."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers)})
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return answer

    def events(self):
        return [json.loads(c["body"]) for c in self.calls]


def outbox(send, clock=None, **kw):
    return Outbox(URL, SECRET, send=send, jitter=False, clock=clock or Clock(), **kw)


# ---- signatures ------------------------------------------------------------------------------------


def test_the_signature_is_the_spec_construction():
    body, t = b'{"type":"order_create"}', 1_800_000_000
    expected = hmac.new(SECRET, b"1800000000." + body, hashlib.sha256).hexdigest()
    assert sign(SECRET, body, t) == f"t={t},v1={expected}"
    assert verify(SECRET, body, sign(SECRET, body, t), now=t + 299) == (t, expected)


@pytest.mark.parametrize("header, message", [
    (None, "Missing Merchant-Signature header."),
    ("", "Missing Merchant-Signature header."),
    ("v1=" + "a" * 64, "Merchant-Signature must be t=<timestamp>,v1=<64_hex>."),
    ("t=1800000000,v1=" + "a" * 63, "Merchant-Signature must be t=<timestamp>,v1=<64_hex>."),
    ("t=1800000000,v0=" + "a" * 64, "Merchant-Signature must be t=<timestamp>,v1=<64_hex>."),
    ("t=abc,v1=" + "a" * 64, "Merchant-Signature must be t=<timestamp>,v1=<64_hex>."),
])
def test_a_missing_or_malformed_header_is_refused(header, message):
    with pytest.raises(SignatureError) as exc:
        verify(SECRET, b"{}", header, now=1_800_000_000)
    assert str(exc.value) == message


@pytest.mark.parametrize("skew", [301, -301, 86_400])
def test_a_stale_or_future_timestamp_is_refused(skew):
    t = 1_800_000_000
    with pytest.raises(SignatureError, match="Timestamp outside allowed window"):
        verify(SECRET, b"{}", sign(SECRET, b"{}", t), now=t + skew)


def test_a_changed_body_or_another_secret_fails_verification():
    t, body = 1_800_000_000, b'{"status":"created"}'
    header = sign(SECRET, body, t)
    for secret, data in ((SECRET, b'{"status":"canceled"}'), (b"x" * 32, body)):
        with pytest.raises(SignatureError, match="verification failed"):
            verify(secret, data, header, now=t)
    assert verify(SECRET, body, header.upper().replace("T=", "t=").replace("V1=", "v1="), now=t)  # hex case


def test_the_secret_comes_from_the_environment_and_must_be_long(monkeypatch):
    monkeypatch.delenv(webhooks.SECRET_ENV, raising=False)
    with pytest.raises(ValueError, match="set SHIPJACK_WEBHOOK_SECRET"):
        secret_from_env()
    monkeypatch.setenv(webhooks.SECRET_ENV, "short")
    with pytest.raises(ValueError, match="shorter than 32 bytes"):
        secret_from_env()
    monkeypatch.setenv(webhooks.SECRET_ENV, "k" * 64)
    assert secret_from_env() == b"k" * 64


@pytest.mark.parametrize("url", ["http://platform.example/hook", "ftp://x/y", "https://", "platform.example"])
def test_signed_order_data_leaves_only_over_https(url):
    with pytest.raises(ValueError, match="https"):
        Outbox(url, SECRET)


def test_plain_http_is_allowed_only_to_this_machine():
    assert Outbox("http://127.0.0.1:9000/hook", SECRET) and Outbox("http://localhost/hook", SECRET)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Outbox(URL, b"short")


# ---- what the events carry ---------------------------------------------------------------------------


def lifecycle_events(world, steps):
    box = outbox(Platform())
    m = world.merchants["northwind"]
    box.attach(m)
    m, oid = buy(world, *steps.get("buy", ()))
    for step, args in steps.get("then", []):
        getattr(m, step)(oid, **args)
    box.run_once()
    box.detach(m)
    box.stop()
    return m, oid, box.send.events()


def test_every_event_of_a_shipped_order_matches_the_acp_schema(world):
    _, _, events = lifecycle_events(world, {"then": [
        ("refund_order", {"amount": 500}),
        ("fulfill_order", {"carrier": "USPS", "tracking_number": "9400111899223456789012",
                           "tracking_url": "https://tools.usps.com/go/TrackConfirmAction?tLabels=9400"}),
        ("mark_delivered", {})]})
    assert [(e["type"], e["data"]["status"]) for e in events] == [
        ("order_create", "created"), ("order_update", "created"), ("order_update", "shipped"),
        ("order_update", "completed")]
    for event in events:
        assert schema_errors(event) == []
    shipped = events[2]["data"]
    assert shipped["line_items"][0]["quantity"] == {"ordered": 1, "current": 1, "fulfilled": 1}
    assert shipped["line_items"][0]["status"] == "fulfilled"
    assert shipped["fulfillments"][0]["destination"]["line_one"] == "1200 W Harrison St"
    assert shipped["fulfillments"][0]["carrier"] == "USPS"
    done = events[3]["data"]
    assert [e["type"] for e in done["fulfillments"][0]["events"]] == ["shipped", "delivered"]
    assert done["adjustments"] == [dict(done["adjustments"][0], type="refund", amount=500, currency="usd",
                                        status="completed")]
    assert done["totals"][-1] == {"type": "amount_refunded", "display_text": "Refunded", "amount": 500}


def test_events_are_full_snapshots_not_deltas(world):
    _, _, events = lifecycle_events(world, {"then": [("refund_order", {"amount": 100})]})
    update = events[1]["data"]
    assert update["line_items"] and update["totals"] and update["fulfillments"] == []
    assert update["checkout_session_id"] == events[0]["data"]["checkout_session_id"]


def test_a_canceled_order_is_refunded_and_its_lines_removed(world):
    _, _, events = lifecycle_events(world, {"then": [("refund_order", {"amount": 1000}), ("cancel_order", {})]})
    last = events[-1]["data"]
    assert last["status"] == "canceled" and schema_errors(events[-1]) == []
    assert [a["amount"] for a in last["adjustments"]] == [1000, 2499]  # cancel returned only what was left
    assert last["line_items"][0]["quantity"]["current"] == 0 and last["line_items"][0]["status"] == "removed"
    assert world.processor.charges[0].refunded == 3499


def test_a_digital_order_completes_when_delivered_and_mixed_orders_ship(world):
    _, _, events = lifecycle_events(world, {"buy": [((GIFT.id, 1),), {"email": EMAIL}],
                                            "then": [("fulfill_order", {})]})
    assert events[-1]["data"]["status"] == "completed" and schema_errors(events[-1]) == []
    assert events[-1]["data"]["fulfillments"][0]["type"] == "digital"
    _, _, mixed = lifecycle_events(world, {"buy": [((HUB.id, 2), (GIFT.id, 1)), {"address": HOME, "email": EMAIL}],
                                           "then": [("fulfill_order", {"carrier": "UPS"})]})
    data = mixed[-1]["data"]
    assert data["status"] == "shipped" and schema_errors(mixed[-1]) == []
    assert sorted(f["type"] for f in data["fulfillments"]) == ["digital", "shipping"]
    assert [li["quantity"]["fulfilled"] for li in data["line_items"]] == [2, 1]


def test_no_event_carries_a_card_number_or_key(world):
    _, _, events = lifecycle_events(world, {"then": [("refund_order", {}),]})
    text = json.dumps(events)
    assert "4242424242424242" not in text and "BEGIN" not in text and '"d"' not in text


def test_the_lifecycle_refuses_steps_out_of_order(world):
    m, oid = buy(world)
    with pytest.raises(Exception, match="not shipped"):
        m.mark_delivered(oid)
    m.fulfill_order(oid)
    for step in (m.fulfill_order, m.cancel_order):
        with pytest.raises(Exception, match="already shipped"):
            step(oid)
    with pytest.raises(Exception, match="https"):
        buy(world)[0].fulfill_order(m.orders[-1]["id"], tracking_url="http://tracker.example/1")
    with pytest.raises(Exception, match="text"):
        m.fulfill_order(m.orders[-1]["id"], carrier=7)


# ---- delivery ---------------------------------------------------------------------------------------


def test_a_delivered_event_is_signed_with_a_stable_request_id(world):
    platform = Platform(200)
    box = outbox(platform).attach(world.merchants["northwind"])
    buy(world)
    assert box.run_once() == 1 and box.pending() == 0
    call = platform.calls[0]
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Request-Id"] == box.delivered[0].id
    t, _ = verify(SECRET, call["body"], call["headers"]["Merchant-Signature"], now=box.clock())
    assert t == int(box.clock())


def test_a_transient_failure_is_retried_with_backoff_and_the_same_request_id(world):
    clock = Clock()
    platform = Platform(503, ConnectionError("reset"), 429, 200)
    box = outbox(platform, clock, base_delay=1.0).attach(world.merchants["northwind"])
    buy(world)
    waits = []
    for _ in range(4):
        box.run_once()
        if box.pending():
            waits.append(box.queue.pending()[0].next_at - clock.t)
            clock.t += 1000
    assert waits == [1.0, 2.0, 4.0] and box.pending() == 0 and len(box.delivered) == 1
    assert len({c["headers"]["Request-Id"] for c in platform.calls}) == 1
    assert len({c["headers"]["Merchant-Signature"] for c in platform.calls}) == 4  # re-signed each try
    assert [r["outcome"] for r in box.attempts_log] == ["retry", "retry", "retry", "delivered"]


def test_not_due_yet_means_not_sent(world):
    clock = Clock()
    platform = Platform(500, 200)
    box = outbox(platform, clock).attach(world.merchants["northwind"])
    buy(world)
    box.run_once()
    box.run_once()  # backoff not over: nothing sent
    assert len(platform.calls) == 1
    clock.t += 1
    box.run_once()
    assert len(platform.calls) == 2 and box.pending() == 0


def test_backoff_is_capped(world):
    clock = Clock()
    box = outbox(Platform(*[500] * 20), clock, base_delay=1.0, max_delay=30.0, max_attempts=20)
    box.attach(world.merchants["northwind"])
    buy(world)
    waits = []
    for _ in range(8):
        box.run_once()
        waits.append(box.queue.pending()[0].next_at - clock.t)
        clock.t += 1000
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30]


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422, 302])
def test_a_rejected_event_is_dead_at_once_and_does_not_block_the_next(world, status):
    platform = Platform(status, 200)
    box = outbox(platform).attach(world.merchants["northwind"])
    m, oid = buy(world)
    m.refund_order(oid, 100)
    box.run_once()
    assert [d.type for d in box.dead] == ["order_create"] and box.dead[0].attempts == 1
    assert [d.type for d in box.delivered] == ["order_update"]


def test_an_event_dies_after_max_attempts(world):
    clock = Clock()
    box = outbox(Platform(*[503] * 10), clock, max_attempts=3).attach(world.merchants["northwind"])
    buy(world)
    for _ in range(5):
        box.run_once()
        clock.t += 1000
    assert len(box.dead) == 1 and box.dead[0].attempts == 3 and box.pending() == 0


def test_one_orders_failure_holds_its_later_events_but_not_other_orders(world):
    clock = Clock()

    def send(url, body, headers, timeout):
        event = json.loads(body)
        sent.append((event["data"]["id"], event["type"], event["data"]["status"]))
        return 503 if event["data"]["id"] == first and failing else 200

    sent, failing = [], True
    box = outbox(send, clock).attach(world.merchants["northwind"])
    m, first = buy(world)
    m.fulfill_order(first)
    _, second = buy(world)
    box.run_once()
    assert (first, "order_update", "shipped") not in sent  # held behind its failed create
    assert (second, "order_create", "created") in sent      # another order is not held
    failing = False
    clock.t += 1000
    box.run_once()
    ours = [s for s in sent if s[0] == first]
    assert ours == [(first, "order_create", "created"), (first, "order_create", "created"),
                    (first, "order_update", "shipped")]


def test_events_queued_from_many_threads_arrive_once_each_in_order(world):
    platform = Platform()
    box = outbox(platform).attach(world.merchants["northwind"])
    m = world.merchants["northwind"]

    def shopper():
        _, oid = buy(world)
        m.refund_order(oid, 100)
        m.fulfill_order(oid)
        m.mark_delivered(oid)

    stop = threading.Event()

    def deliver():
        while not stop.is_set():
            box.run_once()

    runner = threading.Thread(target=deliver)
    runner.start()
    threads = [threading.Thread(target=shopper) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    stop.set()
    runner.join()
    box.run_once()
    events = platform.events()
    assert len(events) == 80 and len({c["headers"]["Request-Id"] for c in platform.calls}) == 80
    by_order = {}
    for e in events:
        by_order.setdefault(e["data"]["id"], []).append(e["data"]["status"])
    assert len(by_order) == 20
    assert all(s == ["created", "created", "shipped", "completed"] for s in by_order.values())


# ---- the receiver ------------------------------------------------------------------------------------


@pytest.fixture
def receiver():
    app = create_receiver(SECRET)
    return TestClient(app), app.state.receiver


def post(client, event, *, secret=SECRET, t=None, request_id="req_1", header=None, raw=None):
    body = raw if raw is not None else json.dumps(event).encode()
    t = int(time.time()) if t is None else t
    headers = {"Content-Type": "application/json", "Request-Id": request_id,
               "Merchant-Signature": header if header is not None else sign(secret, body, t)}
    return client.post(RECEIVER_PATH, content=body, headers=headers)


def order_event(status="created", kind="order_create", oid="ord_1"):
    return {"type": kind, "data": {"type": "order", "id": oid, "checkout_session_id": "cs_1",
                                   "permalink_url": "https://northwind.example/orders/cs_1", "status": status}}


def test_the_receiver_accepts_a_good_event(receiver):
    client, state = receiver
    r = post(client, order_event())
    assert r.status_code == 200 and r.json() == {"received": True, "request_id": "req_1"}
    assert state.orders["ord_1"][1]["status"] == "created"


@pytest.mark.parametrize("case", ["missing", "malformed", "stale", "wrong_secret", "tampered"])
def test_the_receiver_refuses_a_bad_signature_with_401(receiver, case):
    client, state = receiver
    body = json.dumps(order_event()).encode()
    now = int(time.time())
    header = {"missing": "", "malformed": "sha256=abc", "stale": sign(SECRET, body, now - 301),
              "wrong_secret": sign(b"w" * 32, body, now), "tampered": sign(SECRET, body, now)}[case]
    raw = body.replace(b"created", b"canceled") if case == "tampered" else body
    r = post(client, None, header=header, raw=raw)
    assert r.status_code == 401 and r.json()["code"] == "invalid_signature" and not state.orders


def test_a_replay_with_a_new_request_id_is_not_applied_twice(receiver):
    """Request-Id is outside the signature, so the receiver also remembers signatures."""
    client, state = receiver
    body, t = json.dumps(order_event()).encode(), int(time.time())
    header = sign(SECRET, body, t)
    for rid in ("req_a", "req_b", "req_c"):
        assert post(client, None, raw=body, header=header, request_id=rid).status_code == 200
    assert len(state.events) == 1 and state.duplicates == 2


def test_a_retry_of_the_same_event_is_a_duplicate(receiver):
    client, state = receiver
    now = int(time.time())
    post(client, order_event(), t=now - 5, request_id="evt_1")
    post(client, order_event(), t=now, request_id="evt_1")  # re-signed retry, same Request-Id
    assert len(state.events) == 1 and state.duplicates == 1


def test_an_older_snapshot_never_replaces_a_newer_one(receiver):
    """A captured 'created' replayed after 'shipped' (inside the window) must not roll the order back."""
    client, state = receiver
    now = int(time.time())
    post(client, order_event("shipped", "order_update"), t=now, request_id="r2")
    post(client, order_event("created"), t=now - 60, request_id="r1")
    assert state.orders["ord_1"][1]["status"] == "shipped" and state.stale == 1


def test_unknown_event_types_and_statuses_are_accepted(receiver):
    client, state = receiver
    r = post(client, order_event("on_hold_by_customs", "order_reviewed"))
    assert r.status_code == 200 and state.orders["ord_1"][1]["status"] == "on_hold_by_customs"


@pytest.mark.parametrize("event, param", [
    ([], "$"), ({"type": 1, "data": {}}, "$.type"), ({"type": "order_create"}, "$.data"),
    ({"type": "order_create", "data": {"type": "refund"}}, "$.data.type"),
    ({"type": "order_create", "data": {"type": "order", "id": "o", "checkout_session_id": "c",
                                       "permalink_url": "u"}}, "$.data.status"),
])
def test_a_signed_but_malformed_event_is_a_400(receiver, event, param):
    client, state = receiver
    r = post(client, event)
    assert r.status_code == 400 and r.json()["param"] == param and not state.events


def test_a_non_json_body_is_a_400(receiver):
    client, _ = receiver
    raw = b"not json"
    assert post(client, None, raw=raw).status_code == 400
    r = client.post(RECEIVER_PATH, content=b"{}", headers={"Merchant-Signature": sign(SECRET, b"{}", int(time.time())),
                                                           "Content-Type": "text/plain"})
    assert r.status_code == 400


# ---- end to end over real HTTP -----------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_receiver():
    uvicorn = pytest.importorskip("uvicorn")
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


def test_a_purchase_reaches_a_real_receiver_over_http(world, live_receiver):
    url, state = live_receiver
    box = Outbox(url, SECRET).attach(world.merchants["northwind"])
    m, oid = buy(world)
    m.fulfill_order(oid, carrier="USPS", tracking_number="9400")
    m.mark_delivered(oid)
    assert box.drain(timeout=10)
    assert [e["data"]["status"] for e in state.events] == ["created", "shipped", "completed"]
    assert all(schema_errors(e) == [] for e in state.events) and not box.dead


def test_the_demo_store_sends_webhooks_when_asked(monkeypatch, live_receiver):
    url, state = live_receiver
    monkeypatch.setenv(webhooks.SECRET_ENV, SECRET.decode())
    app = demo_app(webhook_url=url)
    try:
        api = TestClient(app)
        h = {"Authorization": "Bearer demo-token", "API-Version": "2026-04-17", "Idempotency-Key": "a"}
        s = api.post("/checkout_sessions", json={"items": [{"id": HUB.id}], "fulfillment_details": {"address": HOME}},
                     headers=h).json()
        done = api.post(f"/checkout_sessions/{s['id']}/complete", json={"payment_data": CARD},
                        headers=dict(h, **{"Idempotency-Key": "b"})).json()
        oid = done["order"]["id"]
        op = dict(h, Authorization="Bearer demo-operator", **{"Idempotency-Key": "c"})
        assert api.post(f"/orders/{oid}/fulfill", json={"carrier": "UPS"}, headers=op).status_code == 200
        deadline = time.monotonic() + 10
        while len(state.events) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [e["data"]["status"] for e in state.events] == ["created", "shipped"]
    finally:
        app.state.outbox.stop()


def test_the_demo_store_needs_the_secret_for_webhooks(monkeypatch):
    monkeypatch.delenv(webhooks.SECRET_ENV, raising=False)
    with pytest.raises(ValueError, match="SHIPJACK_WEBHOOK_SECRET"):
        demo_app(webhook_url="http://127.0.0.1:9/hook")
