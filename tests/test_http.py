"""The ACP REST API: headers, idempotency, errors, and a full AP2 purchase over HTTP."""

import pytest
from fastapi.testclient import TestClient

from agent_checkout.http import create_app
from agent_checkout.merchant import ACP_API_VERSION
from agent_checkout.remote import RemoteMerchant

from conftest import DOCK, HOME, HUB, hub_intent

TOKEN = "platform-token"


@pytest.fixture
def api(world):
    return TestClient(create_app(world.merchants["northwind"], {TOKEN: "agent-platform"}))


def headers(key="k1", **extra):
    return {"Authorization": f"Bearer {TOKEN}", "API-Version": ACP_API_VERSION, "Idempotency-Key": key, **extra}


CREATE = {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": HOME}}


def test_auth_and_version_are_required(api):
    assert api.post("/checkout_sessions", json=CREATE, headers={"API-Version": ACP_API_VERSION,
                                                                "Idempotency-Key": "x"}).status_code == 401
    missing = api.post("/checkout_sessions", json=CREATE, headers={"Authorization": f"Bearer {TOKEN}",
                                                                   "Idempotency-Key": "x"})
    assert missing.status_code == 400 and missing.json()["supported_versions"] == [ACP_API_VERSION]
    old = api.post("/checkout_sessions", json=CREATE, headers=headers(**{"API-Version": "2025-09-29"}))
    assert old.json()["code"] == "unsupported_api_version"


def test_post_needs_an_idempotency_key(api):
    h = headers()
    del h["Idempotency-Key"]
    r = api.post("/checkout_sessions", json=CREATE, headers=h)
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")


def test_create_is_201_and_replays_by_key(api, world):
    first = api.post("/checkout_sessions", json=CREATE, headers=headers("same"))
    assert first.status_code == 201 and first.json()["status"] == "ready_for_payment"
    reordered = {"fulfillment_details": {"address": HOME}, "items": [{"quantity": 1.0, "id": HUB.id}]}
    again = api.post("/checkout_sessions", json=reordered, headers=headers("same"))
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json() and len(world.merchants["northwind"].sessions) == 1
    conflict = api.post("/checkout_sessions", json={**CREATE, "items": [{"id": HUB.id, "quantity": 2}]},
                        headers=headers("same"))
    assert (conflict.status_code, conflict.json()["code"]) == (422, "idempotency_conflict")


def test_errors_are_flat_and_cancel_twice_is_405(api):
    missing = api.get("/checkout_sessions/cs_nope", headers=headers())
    assert missing.status_code == 404 and set(missing.json()) >= {"type", "code", "message"}
    sid = api.post("/checkout_sessions", json=CREATE, headers=headers("c1")).json()["id"]
    assert api.post(f"/checkout_sessions/{sid}/cancel", json={}, headers=headers("c2")).status_code == 200
    assert api.post(f"/checkout_sessions/{sid}/cancel", json={}, headers=headers("c3")).status_code == 405


def test_ap2_purchase_over_http(api, world):
    remote = RemoteMerchant(api, TOKEN)
    for address, expected in ((HOME, "paid"), (DOCK, "declined")):
        wallet = world.wallet("ap2+ext", hub_intent(world))
        wallet.directory = {remote.id: remote.verification_key()}  # the key the store publishes
        session = remote.create_session([{"id": HUB.id, "quantity": 1}], fulfillment_details={"address": address})
        result = wallet.pay(remote, session["id"])
        assert result["status"] == expected, result
    assert [c.account for c in world.processor.charges] == ["acct_northwind"]
    assert result["stage"] == "wallet" and "77 Dock St" in result["message"]
