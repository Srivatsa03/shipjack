"""The ACP checkout API on awkward input: bad bodies, odd types, repeats, races of state, server faults.

Rules under test: a bad request is a 4xx with a flat error and never a 500; a retry never charges
twice; nothing a client sends can leave the store in a state that pays a different amount than it
showed.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import DOCK, HOME, HUB
from shipjack.http import create_app
from shipjack.merchant import ACP_API_VERSION

TOKEN, OTHER = "platform-token", "second-platform-token"
GOOD = {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": HOME}}


@pytest.fixture
def api(world):
    app = create_app(world.merchants["northwind"], {TOKEN: "platform-a", OTHER: "platform-b"})
    return TestClient(app, raise_server_exceptions=False)


def h(key="k", token=TOKEN, **extra):
    return {"Authorization": f"Bearer {token}", "API-Version": ACP_API_VERSION, "Idempotency-Key": key, **extra}


def post(api, path, body, key="k", token=TOKEN, **extra):
    return api.post(path, json=body, headers=h(key, token, **extra))


def flat(resp):
    body = resp.json()
    assert set(body) >= {"type", "code", "message"}, body
    return body


# ---- bad bodies are 4xx, never 500 ----------------------------------------------------------------


@pytest.mark.parametrize("body", [
    [], "text", 7, None, True,
    {"items": "hub"}, {"items": {"id": HUB.id}}, {"items": [None]}, {"items": ["nw-hub-7"]}, {"items": [[1]]},
    {"items": [{"quantity": 1}]}, {"items": [{"id": None, "quantity": 1}]}, {"items": [{"id": ["a"], "quantity": 1}]},
    {"items": [{"id": HUB.id, "quantity": "many"}]}, {"items": [{"id": HUB.id, "quantity": None}]},
    {"items": [{"id": HUB.id, "quantity": 0}]}, {"items": [{"id": HUB.id, "quantity": -3}]},
    {"items": [{"id": HUB.id, "quantity": 1.5}]}, {"items": [{"id": HUB.id, "quantity": True}]},
    {"items": [{"id": HUB.id, "quantity": [1]}]}, {"items": [{"id": "no-such-product", "quantity": 1}]},
    {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": "home"},
    {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": ["home"]},
    {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": "1200 W Harrison St"}},
    {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"address": {"line_one": ["x"]}}},
    {"items": [{"id": HUB.id, "quantity": 1}], "fulfillment_details": {"email": ["a@b.c"]}},
    {"items": [{"id": HUB.id, "quantity": 1}], "buyer": "me"},
])
def test_a_malformed_create_is_a_flat_4xx(api, body):
    r = post(api, "/checkout_sessions", body)
    assert 400 <= r.status_code < 500, (body, r.status_code, r.text[:200])
    flat(r)


def test_invalid_json_is_a_flat_400(api):
    r = api.post("/checkout_sessions", content=b"{not json", headers={**h(), "Content-Type": "application/json"})
    assert r.status_code == 400 and flat(r)["code"] == "invalid"


def test_an_absurd_quantity_is_refused_or_priced_exactly(api, world):
    r = post(api, "/checkout_sessions", {**GOOD, "items": [{"id": HUB.id, "quantity": 10**12}]})
    assert r.status_code in (201, 400)
    if r.status_code == 201:
        total = next(t["amount"] for t in r.json()["totals"] if t["type"] == "total")
        assert total == 3499 * 10**12  # integers do not round or wrap
    assert not world.processor.charges


def test_whitespace_and_case_in_ids_are_not_guessed(api):
    for pid in (" nw-hub-7", "NW-HUB-7", "nw-hub-7\n"):
        r = post(api, "/checkout_sessions", {**GOOD, "items": [{"id": pid, "quantity": 1}]}, key=pid)
        assert r.status_code == 400, pid


# ---- state after the first completion ------------------------------------------------------------


def paid(api):
    sid = post(api, "/checkout_sessions", GOOD, key="create").json()["id"]
    return sid, post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {
        "handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}}, key="pay")


def test_a_replayed_complete_does_not_charge_twice(api, world):
    sid, first = paid(api)
    assert first.status_code == 200 and first.json()["status"] == "completed"
    again = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {
        "handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}}, key="pay")
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    assert len(world.processor.charges) == 1


def test_a_second_complete_with_a_new_key_is_refused(api, world):
    sid, _ = paid(api)
    r = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {
        "handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}}, key="pay-2")
    assert r.status_code == 400 and "already completed" in flat(r)["message"]
    assert len(world.processor.charges) == 1


def test_nothing_changes_a_finished_session(api, world):
    sid, _ = paid(api)
    assert post(api, f"/checkout_sessions/{sid}", {"items": [{"id": HUB.id, "quantity": 5}]}, key="u").status_code == 400
    assert post(api, f"/checkout_sessions/{sid}", {"fulfillment_details": {"address": DOCK}}, key="u2").status_code == 400
    assert post(api, f"/checkout_sessions/{sid}/cancel", {}, key="c").status_code == 405
    assert post(api, f"/checkout_sessions/{sid}/ap2", {}, key="a").status_code == 400
    order = world.merchants["northwind"].orders[0]
    assert order["amount"] == 3499 and order["fulfillment_details"]["address"]["line_one"] == "1200 W Harrison St"


def test_a_canceled_session_cannot_be_completed(api, world):
    sid = post(api, "/checkout_sessions", GOOD, key="c1").json()["id"]
    assert post(api, f"/checkout_sessions/{sid}/cancel", {}, key="c2").status_code == 200
    r = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {"handler_id": "card"}}, key="c3")
    assert r.status_code == 400 and not world.processor.charges


def test_completing_a_cart_that_is_not_ready_is_refused(api, world):
    sid = post(api, "/checkout_sessions", {"items": [{"id": HUB.id, "quantity": 1}]}, key="n1").json()["id"]  # no address
    assert api.get(f"/checkout_sessions/{sid}", headers=h()).json()["status"] == "not_ready_for_payment"
    r = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {"handler_id": "card"}}, key="n2")
    assert r.status_code == 400 and flat(r)["param"] == "$.status" and not world.processor.charges


@pytest.mark.parametrize("payment_data", [None, [], "card", {}, {"handler_id": None}, {"handler_id": "wire"},
                                          {"handler_id": ["card"]}, {"handler_id": "card", "instrument": "x"},
                                          {"handler_id": "card", "instrument": {"credential": "x"}},
                                          {"handler_id": "ap2"}, {"handler_id": "ap2", "checkout_mandate": 5}])
def test_a_malformed_payment_never_pays_and_never_500s(api, world, payment_data):
    sid = post(api, "/checkout_sessions", GOOD, key="p1").json()["id"]
    r = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": payment_data}, key="p2")
    assert r.status_code < 500, (payment_data, r.text[:200])
    assert not world.processor.charges and not world.merchants["northwind"].orders


def test_a_wrong_card_number_is_declined_not_charged(api, world):
    sid = post(api, "/checkout_sessions", GOOD, key="w1").json()["id"]
    r = post(api, f"/checkout_sessions/{sid}/complete", {"payment_data": {
        "handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4000000000000002"}}}}, key="w2")
    assert r.json()["error"]["code"] == "payment_declined" and not world.processor.charges


# ---- changing the cart invalidates the signed checkout -------------------------------------------


def test_every_change_to_a_cart_drops_the_signed_checkout(api, world):
    sid = post(api, "/checkout_sessions", GOOD, key="s1").json()["id"]
    merchant = world.merchants["northwind"]
    for body in ({"items": [{"id": HUB.id, "quantity": 2}]}, {"fulfillment_details": {"address": DOCK}},
                 {"buyer": {"email": "x@example.com"}}):
        signed = post(api, f"/checkout_sessions/{sid}/ap2", {}, key=f"a-{len(str(body))}").json()
        assert merchant.sessions[sid].checkout_jwt == signed["checkout_jwt"]
        post(api, f"/checkout_sessions/{sid}", body, key=f"u-{len(str(body))}")
        assert merchant.sessions[sid].checkout_jwt is None and merchant.sessions[sid].nonce is None


def test_signing_twice_replaces_the_nonce(api, world):
    sid = post(api, "/checkout_sessions", GOOD, key="t1").json()["id"]
    first = post(api, f"/checkout_sessions/{sid}/ap2", {}, key="t2").json()
    second = post(api, f"/checkout_sessions/{sid}/ap2", {}, key="t3").json()
    assert first["nonce"] != second["nonce"]
    assert world.merchants["northwind"].sessions[sid].nonce == second["nonce"]


# ---- idempotency scope ---------------------------------------------------------------------------


def test_the_same_key_from_another_caller_is_a_different_request(api, world):
    a = post(api, "/checkout_sessions", GOOD, key="shared", token=TOKEN)
    b = post(api, "/checkout_sessions", GOOD, key="shared", token=OTHER)
    assert a.json()["id"] != b.json()["id"] and "Idempotent-Replayed" not in b.headers


def test_the_same_key_on_another_path_is_a_different_request(api):
    sid = post(api, "/checkout_sessions", GOOD, key="one").json()["id"]
    r = post(api, f"/checkout_sessions/{sid}", {"items": [{"id": HUB.id, "quantity": 2}]}, key="one")
    assert r.status_code == 200 and "Idempotent-Replayed" not in r.headers


def test_a_bad_request_is_remembered_by_its_key_too(api):
    bad = post(api, "/checkout_sessions", {"items": [{"id": "ghost", "quantity": 1}]}, key="bad")
    again = post(api, "/checkout_sessions", {"items": [{"id": "ghost", "quantity": 1}]}, key="bad")
    assert bad.status_code == again.status_code == 400 and again.headers["Idempotent-Replayed"] == "true"
    changed = post(api, "/checkout_sessions", GOOD, key="bad")
    assert changed.status_code == 422 and flat(changed)["code"] == "idempotency_conflict"


def test_null_and_a_missing_field_are_different_requests(api):
    post(api, "/checkout_sessions", {**GOOD, "buyer": None}, key="nulls")
    r = post(api, "/checkout_sessions", GOOD, key="nulls")
    assert r.status_code == 422


def test_an_over_long_key_is_refused(api):
    assert post(api, "/checkout_sessions", GOOD, key="k" * 256).status_code == 400
    assert post(api, "/checkout_sessions", GOOD, key="k" * 255).status_code == 201


# ---- headers --------------------------------------------------------------------------------------


@pytest.mark.parametrize("auth", ["", "Bearer", "Bearer ", "Basic abc", f"bearer {TOKEN}", f"Bearer  {TOKEN}x", TOKEN])
def test_only_a_well_formed_known_bearer_token_gets_in(api, auth):
    r = api.post("/checkout_sessions", json=GOOD, headers={"Authorization": auth, "API-Version": ACP_API_VERSION,
                                                          "Idempotency-Key": "z"})
    assert r.status_code == 401 and flat(r)["code"] == "unauthorized"


def test_reads_need_auth_and_a_version_too(api):
    assert api.get("/products").status_code == 401
    assert api.get("/products", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400
    assert api.get("/products?q=hub", headers=h()).status_code == 200
    assert api.get("/checkout_sessions/cs_ghost", headers=h()).status_code == 404


def test_the_request_id_is_echoed(api):
    r = post(api, "/checkout_sessions", GOOD, key="rid", **{"Request-Id": "req-123"})
    assert r.headers["Request-Id"] == "req-123"


def test_the_published_key_needs_no_auth_and_is_public_only(api):
    r = api.get("/.well-known/ap2-merchant-key")
    assert r.status_code == 200 and "d" not in r.json()["jwk"] and r.json()["jwk"]["kty"] == "EC"


# ---- server faults --------------------------------------------------------------------------------


def test_an_unexpected_fault_is_a_flat_500_and_is_not_remembered(api, world, monkeypatch):
    merchant = world.merchants["northwind"]
    real = merchant.create_session
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database fell over")
        return real(*a, **kw)

    monkeypatch.setattr(merchant, "create_session", flaky)
    first = post(api, "/checkout_sessions", GOOD, key="retry")
    assert first.status_code == 500 and flat(first)["type"] == "processing_error"
    assert "database" not in first.text  # no internals leak
    second = post(api, "/checkout_sessions", GOOD, key="retry")
    assert second.status_code == 201 and "Idempotent-Replayed" not in second.headers  # 5xx is never cached


def test_acp_discovery_lists_transports_and_extensions(api):
    from shipjack.mandates import FULFILLMENT
    r = api.get("/.well-known/acp.json")  # discovery needs no auth
    assert r.status_code == 200
    d = r.json()
    assert d["seller"]["id"] == "northwind" and set(d["transports"]) == {"rest", "mcp"}
    assert ACP_API_VERSION in d["protocol"]["supported_versions"]
    assert "ap2" in d["payment"]["handlers"] and FULFILLMENT in d["extensions"]
