"""The assistant API end to end: SSE turns, the shopper's approval, the operator's refund approval, the
store's signed webhooks, and access rules. Needs Postgres (SHIPJACK_TEST_PG); each test gets its own
schema. The store runs in process; the models are scripted.
"""

import json
import time
import uuid

import pytest
from conftest import PG
from fastapi.testclient import TestClient
from test_assistant import Shop, buy_hub_script, call, say

from shipjack.assistant.api import Settings, create_app
from shipjack.webhooks import sign

pytestmark = pytest.mark.skipif(not PG, reason="set SHIPJACK_TEST_PG to a Postgres URL")
SHOPPER = {"Authorization": "Bearer demo-shopper"}
OPS = {"Authorization": "Bearer demo-operator"}
SECRET = b"w" * 32


@pytest.fixture
def api_for():
    schemas, clients = [], []

    def make(script):
        shop = Shop(script)
        schema = f"a_{uuid.uuid4().hex[:10]}"
        schemas.append(schema)
        app = create_app(Settings(db_url=PG, schema=schema, webhook_secret=SECRET, sweep_every_s=0.2), deps=shop.deps)
        client = TestClient(app)
        client.schema = schema
        client.__enter__()
        clients.append(client)
        return client, shop

    yield make
    for client in clients:
        client.__exit__(None, None, None)
    import psycopg
    with psycopg.connect(PG, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def sse(response) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[6:])))
    return events


def turn(client, thread_id, text):
    with client.stream("POST", f"/api/threads/{thread_id}/messages", json={"text": text}, headers=SHOPPER) as r:
        assert r.status_code == 200, r.read()
        return sse(r)


def test_routes_need_the_right_person(api_for):
    client, _ = api_for([])
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=SHOPPER).json()["name"] == "Sam Rivera"
    assert client.get("/api/ops/approvals", headers=SHOPPER).status_code == 403
    assert client.get("/api/ops/metrics", headers=OPS).status_code == 200


def test_a_purchase_streams_pauses_for_the_shopper_and_completes(api_for):
    client, shop = api_for(buy_hub_script())
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    events = turn(client, thread, "find me a usb-c hub and buy it")
    kinds = [e for e, _ in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and "trace" in kinds
    [card] = [d for e, d in events if e == "approval"]
    assert card["kind"] == "purchase" and card["total"] == 3499 and card["ship_to"]["city"] == "Chicago"
    assert not shop.world.processor.charges
    view = client.get(f"/api/threads/{thread}", headers=SHOPPER).json()
    assert view["pending"][0]["id"] == card["id"] and view["title"] == "find me a usb-c hub and buy it"

    with client.stream("POST", f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                       headers=SHOPPER) as r:
        resumed = sse(r)
    final = [d for e, d in resumed if e == "node" and d.get("node") == "pay"][0]
    assert "is placed for $34.99" in final["messages"][0]["content"]
    assert len(shop.world.processor.charges) == 1
    again = client.post(f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                        headers=SHOPPER)
    assert again.status_code in (404, 409) and len(shop.world.processor.charges) == 1
    assert [o["id"] for o in client.get("/api/orders", headers=SHOPPER).json()] == ["ord_cs_northwind_1"]
    stored = client.get(f"/api/threads/{thread}/events", headers=SHOPPER).json()
    assert {"route", "model_call", "checkout_ready", "approval_requested", "order_placed"} <= {e["event"] for e in stored}
    metrics = client.get("/api/ops/metrics", headers=OPS).json()
    assert metrics["orders"] == 1 and metrics["model_calls"] >= 4 and metrics["tokens_in"] > 0


def test_another_users_thread_is_invisible(api_for):
    import psycopg
    client, _ = api_for([])
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute(f'INSERT INTO "{client.schema}".users (id, name, email, token) VALUES (%s, %s, %s, %s)',
                     ("u_eve", "Eve", "eve@example.com", "eve-token"))
    eve = {"Authorization": "Bearer eve-token"}
    assert client.get(f"/api/threads/{thread}", headers=eve).status_code == 404
    assert client.post(f"/api/threads/{thread}/messages", json={"text": "hi"}, headers=eve).status_code == 404
    assert client.get(f"/api/threads/{thread}/events", headers=eve).status_code == 404


def test_a_refund_waits_for_the_operator_and_resumes_in_the_background(api_for):
    client, shop = api_for(buy_hub_script() + [
        say("support"), call("request_refund", {"order_id": "ord_cs_northwind_1", "reason": "it arrived broken"})])
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    card = [d for e, d in turn(client, thread, "buy a hub") if e == "approval"][0]
    with client.stream("POST", f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                       headers=SHOPPER) as r:
        sse(r)
    ask = [d for e, d in turn(client, thread, "it arrived broken, refund it") if e == "approval"][0]
    assert ask["actor"] == "operator"
    # the shopper cannot approve their own refund
    assert client.post(f"/api/threads/{thread}/decision", json={"approval_id": ask["id"], "approved": True},
                       headers=SHOPPER).status_code == 404
    [pending] = client.get("/api/ops/approvals", headers=OPS).json()
    assert pending["id"] == ask["id"] and pending["payload"]["amount"] == 3499
    r = client.post(f"/api/ops/approvals/{ask['id']}", json={"approved": True, "note": "photo checks out"}, headers=OPS)
    assert r.status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        msgs = client.get(f"/api/threads/{thread}", headers=SHOPPER).json()["messages"]
        if "refunded" in msgs[-1]["content"]:
            break
        time.sleep(0.1)
    assert "canceled and $34.99 is refunded" in msgs[-1]["content"] and shop.world.processor.charges[0].refunded == 3499
    assert client.post(f"/api/ops/approvals/{ask['id']}", json={"approved": True}, headers=OPS).status_code == 409


def webhook(client, order, t=None, secret=SECRET, request_id=None, kind="order_update"):
    body = json.dumps({"type": kind, "data": order}).encode()
    t = int(time.time()) if t is None else t
    headers = {"Content-Type": "application/json", "Merchant-Signature": sign(secret, body, t),
               "Request-Id": request_id or uuid.uuid4().hex}
    return client.post("/api/webhooks/orders", content=body, headers=headers)


def test_signed_webhooks_keep_the_order_current_and_ignore_replays(api_for):
    client, shop = api_for(buy_hub_script())
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    card = [d for e, d in turn(client, thread, "buy a hub") if e == "approval"][0]
    with client.stream("POST", f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                       headers=SHOPPER) as r:
        sse(r)
    shipped = shop.merchant.fulfill_order("ord_cs_northwind_1", carrier="USPS", tracking_number="9400")
    now = int(time.time())
    assert webhook(client, shipped, t=now).json()["received"] is True
    assert client.get("/api/orders", headers=SHOPPER).json()[0]["status"] == "shipped"
    created = {**shipped, "status": "created"}
    assert webhook(client, created, t=now - 30).status_code == 200  # an older snapshot, replayed
    assert client.get("/api/orders", headers=SHOPPER).json()[0]["status"] == "shipped"
    assert webhook(client, shipped, secret=b"x" * 32).status_code == 401
    assert webhook(client, {"type": "order"}).status_code == 400
    events = client.get(f"/api/threads/{thread}/events", headers=SHOPPER).json()
    assert [e["data"]["status"] for e in events if e["event"] == "order_update"] == ["shipped"]


def test_a_saved_address_is_checked(api_for):
    client, _ = api_for([])
    bad = client.put("/api/me/address", json={"name": "Sam", "line_one": "1 Main", "city": "X"}, headers=SHOPPER)
    assert bad.status_code == 400 and "postal_code is required" in bad.json()["detail"]
    ok = client.put("/api/me/address", json={"name": "Sam Rivera", "line_one": "77 Lake St", "city": "Chicago",
                                              "state": "IL", "postal_code": "60601", "country": "US"}, headers=SHOPPER)
    assert ok.status_code == 200 and client.get("/api/me", headers=SHOPPER).json()["address"]["line_one"] == "77 Lake St"


def test_a_turn_needs_text(api_for):
    client, _ = api_for([])
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    assert client.post(f"/api/threads/{thread}/messages", json={"text": "  "}, headers=SHOPPER).status_code == 400
    assert client.post(f"/api/threads/{thread}/messages", json={"text": "x" * 2001}, headers=SHOPPER).status_code == 400


def test_a_purchase_card_expires_and_is_cancelled(api_for):
    """A card left unanswered can't be paid later: the decision is refused and the sweeper cancels it."""
    import psycopg
    client, shop = api_for(buy_hub_script())
    thread = client.post("/api/threads", headers=SHOPPER).json()["id"]
    card = [d for e, d in turn(client, thread, "buy a hub") if e == "approval"][0]
    with psycopg.connect(PG, autocommit=True) as conn:  # age the card past the 15 minutes
        conn.execute(f"UPDATE \"{client.schema}\".approvals SET created = now() - interval '16 minutes'")
    late = client.post(f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                       headers=SHOPPER)
    assert late.status_code == 409 and not shop.world.processor.charges
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        view = client.get(f"/api/threads/{thread}", headers=SHOPPER).json()
        if not view["pending"]:
            break
        time.sleep(0.2)
    assert "expired before it was approved" in view["messages"][-1]["content"]
    assert shop.merchant.get_session("cs_northwind_1")["status"] == "canceled" and not shop.world.processor.charges
