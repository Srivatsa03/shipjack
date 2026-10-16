"""The public demo: every visitor gets a private sandbox (a shopper and that shopper's store console),
with no shared tokens, daily caps on model calls and a limit on new sessions per network.
Needs Postgres (SHIPJACK_TEST_PG)."""

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import PG
from shipjack.assistant.api import Settings, create_app
from test_assistant import Shop, buy_hub_script, call, say
from test_assistant_api import sse

pytestmark = pytest.mark.skipif(not PG, reason="set SHIPJACK_TEST_PG to a Postgres URL")


@pytest.fixture
def demo():
    made = []

    def make(script, **settings):
        shop = Shop(script)
        schema = f"d_{uuid.uuid4().hex[:10]}"
        app = create_app(Settings(db_url=PG, schema=schema, demo=True, allow_imports=False, **settings), deps=shop.deps)
        client = TestClient(app)
        client.__enter__()
        made.append((client, schema))
        return client, shop

    yield make
    for client, schema in made:
        client.__exit__(None, None, None)
        with psycopg.connect(PG, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def guest(client, ip="203.0.113.7"):
    r = client.post("/api/demo/session", headers={"X-Forwarded-For": ip})
    assert r.status_code == 201, r.text
    s = r.json()
    return {"Authorization": f"Bearer {s['shopper_token']}"}, {"Authorization": f"Bearer {s['operator_token']}"}


def buy(client, shopper):
    thread = client.post("/api/threads", headers=shopper).json()["id"]
    with client.stream("POST", f"/api/threads/{thread}/messages", json={"text": "buy a hub"}, headers=shopper) as r:
        card = [d for e, d in sse(r) if e == "approval"][0]
    with client.stream("POST", f"/api/threads/{thread}/decision", json={"approval_id": card["id"], "approved": True},
                       headers=shopper) as r:
        sse(r)
    return thread


def test_the_demo_has_no_shared_well_known_tokens(demo):
    client, _ = demo([])
    assert client.get("/api/health").json()["demo"] is True
    for token in ("demo-shopper", "demo-operator"):
        assert client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_each_visitor_sees_only_their_own_sandbox(demo):
    client, _ = demo(buy_hub_script() + buy_hub_script())
    alice, alice_ops = guest(client, "198.51.100.1")
    bob, bob_ops = guest(client, "198.51.100.2")
    a_thread = buy(client, alice)
    buy(client, bob)
    assert client.get(f"/api/threads/{a_thread}", headers=bob).status_code == 404
    assert client.get(f"/api/threads/{a_thread}", headers=bob_ops).status_code == 404
    assert client.get(f"/api/threads/{a_thread}/events", headers=bob_ops).status_code == 404
    a_orders = client.get("/api/ops/orders", headers=alice_ops).json()
    b_orders = client.get("/api/ops/orders", headers=bob_ops).json()
    assert len(a_orders) == 1 and len(b_orders) == 1 and a_orders[0]["id"] != b_orders[0]["id"]
    assert [t["id"] for t in client.get("/api/ops/threads", headers=alice_ops).json()] == [a_thread]
    r = client.post(f"/api/ops/orders/{a_orders[0]['id']}/ship", json={}, headers=bob_ops)
    assert r.status_code == 404  # Bob's console cannot ship Alice's order


def test_each_console_counts_only_its_own_sandbox(demo):
    """The console's health view in the demo: one visitor's numbers, not every visitor's."""
    client, _ = demo(buy_hub_script() + buy_hub_script())
    alice, alice_ops = guest(client, "198.51.100.1")
    bob, bob_ops = guest(client, "198.51.100.2")
    buy(client, alice)
    buy(client, bob)
    buy_calls = None
    for ops in (alice_ops, bob_ops):
        m = client.get("/api/ops/metrics", headers=ops).json()
        assert (m["threads"], m["orders"], m["purchases_approved"]) == (1, 1, 1)
        assert m["model_calls"] > 0 and m["model_calls"] == (buy_calls or m["model_calls"])
        buy_calls = m["model_calls"]


def test_an_operator_cannot_decide_another_sandboxes_refund(demo):
    client, _ = demo(buy_hub_script() + [say("support"), call("request_refund", {"order_id": "ord_cs_northwind_1",
                                                                                 "reason": "broken"})])
    alice, alice_ops = guest(client, "198.51.100.3")
    _, bob_ops = guest(client, "198.51.100.4")
    thread = buy(client, alice)
    with client.stream("POST", f"/api/threads/{thread}/messages", json={"text": "refund it"}, headers=alice) as r:
        ask = [d for e, d in sse(r) if e == "approval"][0]
    assert client.get("/api/ops/approvals", headers=bob_ops).json() == []
    assert client.post(f"/api/ops/approvals/{ask['id']}", json={"approved": True}, headers=bob_ops).status_code == 404
    assert [a["id"] for a in client.get("/api/ops/approvals", headers=alice_ops).json()] == [ask["id"]]


def test_new_sessions_are_limited_per_network(demo):
    client, _ = demo([], demo_guests_per_ip_hour=2)
    guest(client, "192.0.2.9")
    guest(client, "192.0.2.9")
    r = client.post("/api/demo/session", headers={"X-Forwarded-For": "192.0.2.9"})
    assert r.status_code == 429
    guest(client, "192.0.2.10")  # another network is fine


def test_a_visitor_stops_at_the_daily_model_budget(demo):
    client, _ = demo([say("chat"), say("Hi!")] * 3, demo_calls_per_guest=2)
    shopper, _ = guest(client)
    thread = client.post("/api/threads", headers=shopper).json()["id"]
    with client.stream("POST", f"/api/threads/{thread}/messages", json={"text": "hello"}, headers=shopper) as r:
        sse(r)  # two model calls: the router and the chat agent
    r = client.post(f"/api/threads/{thread}/messages", json={"text": "hello again"}, headers=shopper)
    assert r.status_code == 429 and "today's demo turns" in r.json()["detail"]


def test_the_whole_demo_stops_at_its_daily_budget(demo):
    client, _ = demo([say("chat"), say("Hi!")] * 3, demo_calls_per_day=2)
    first, _ = guest(client, "192.0.2.20")
    second, _ = guest(client, "192.0.2.21")
    thread = client.post("/api/threads", headers=first).json()["id"]
    with client.stream("POST", f"/api/threads/{thread}/messages", json={"text": "hello"}, headers=first) as r:
        sse(r)
    other = client.post("/api/threads", headers=second).json()["id"]
    r = client.post(f"/api/threads/{other}/messages", json={"text": "hi"}, headers=second)
    assert r.status_code == 429 and "model budget" in r.json()["detail"]


def test_imports_are_off_in_the_demo(demo):
    client, _ = demo([])
    _, ops = guest(client)
    assert client.post("/api/ops/catalog", json={"csv": "id,title,price\na,b,1\n"}, headers=ops).status_code == 403
    assert client.get("/api/ops/knowledge", headers=ops).json()["imports_allowed"] is False
