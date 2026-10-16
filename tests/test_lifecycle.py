"""After the order: refunds, dispute evidence and the audit trail.

The rules: a merchant never refunds more than it took, a refund goes back on the rail that charged,
and every order can be rebuilt from stored evidence (what the user approved, joined to what moved).
"""

import os
import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

import conftest
from conftest import HOME, HUB, hub_intent
from shipjack.direct import Screen, new_user, purchase_direct
from shipjack.http import create_app
from shipjack.mandates import digest
from shipjack.merchant import ACP_API_VERSION, CheckoutError
from shipjack.psp import PaymentError
from shipjack.stripe_psp import StripeProcessor
from shipjack.world import build

CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}


def card_order(world, store="northwind", items=((HUB.id, 1),)):
    m = world.merchants[store]
    s = m.create_session([{"id": p, "quantity": q} for p, q in items], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], CARD)
    return m, out["order"]["id"]


def ap2_order(world):
    wallet = world.wallet("ap2", hub_intent(world))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    assert wallet.pay(m, s["id"])["status"] == "paid"
    return m, m.orders[-1]["id"]


# ---- refunds --------------------------------------------------------------------------------------


def test_a_full_refund_returns_everything_once(world):
    m, oid = card_order(world)
    out = m.refund_order(oid)
    assert out == {"order_id": oid, "refunded": 3499, "total_refunded": 3499, "remaining": 0,
                   "payment_status": "refunded", "psp_ref": ""}
    assert world.processor.charges[0].refunded == 3499 and len(world.processor.refunds) == 1
    with pytest.raises(PaymentError, match="refund amount must be positive"):
        m.refund_order(oid)  # nothing left to refund


def test_partial_refunds_add_up_and_stop_at_the_charge(world):
    m, oid = card_order(world)
    assert m.refund_order(oid, 1000)["payment_status"] == "partially_refunded"
    assert m.refund_order(oid, 2000)["remaining"] == 499
    with pytest.raises(PaymentError, match="over the 499 still refundable"):
        m.refund_order(oid, 500)
    assert m.refund_order(oid, 499)["payment_status"] == "refunded"
    assert sum(r.amount for r in world.processor.refunds) == 3499


@pytest.mark.parametrize("amount", [0, -1])
def test_a_non_positive_refund_is_refused(world, amount):
    m, oid = card_order(world)
    with pytest.raises(PaymentError, match="positive"):
        m.refund_order(oid, amount)
    assert world.processor.charges[0].refunded == 0


def test_refunding_an_unknown_order_is_not_found(world):
    with pytest.raises(CheckoutError, match="no order"):
        world.merchants["northwind"].refund_order("ord_ghost")


def test_two_full_refunds_at_once_return_the_money_once(world, monkeypatch):
    m, oid = card_order(world)
    real = world.processor._refund_rail

    def slow(*a):
        import time
        time.sleep(0.02)  # widen the window between "how much is left" and "record the refund"
        return real(*a)

    monkeypatch.setattr(world.processor, "_refund_rail", slow)
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(m.refund_order(oid))
        except PaymentError as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert world.processor.charges[0].refunded == 3499
    assert sum(1 for r in results if isinstance(r, dict)) == 1


# ---- dispute evidence ----------------------------------------------------------------------------


def test_ap2_evidence_joins_the_signed_checkout_the_mandate_and_the_money(world):
    m, oid = ap2_order(world)
    m.refund_order(oid, 500)
    ev = m.dispute_evidence(oid)
    assert ev["checkout"]["jwt"] and ev["checkout"]["hash"] == digest(ev["checkout"]["jwt"])
    assert ev["mandate_reference"] and ev["mandate_reference"] == ev["receipt"]["claims"]["reference"]
    assert ev["receipt"]["claims"]["status"] == "Success" and ev["receipt"]["claims"]["order_id"] == oid
    assert ev["charge"] == {"amount": 3499, "currency": "USD", "method": "ap2", "psp_ref": "", "refunded": 500}
    assert ev["refunds"] == [{"amount": 500, "currency": "USD", "psp_ref": ""}]
    assert ev["order"]["fulfillment_details"]["address"]["line_one"] == "1200 W Harrison St"


def test_card_evidence_has_no_mandate(world):
    m, oid = card_order(world)
    ev = m.dispute_evidence(oid)
    assert ev["checkout"] == {"jwt": None, "hash": None} and ev["mandate_reference"] == "" and ev["receipt"] is None


def test_direct_evidence_carries_the_users_approval(world):
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    assert purchase_direct(world, "northwind", s["id"], new_user(hub_intent(world)), Screen.WITH_DESTINATION)["status"] == "paid"
    ev = m.dispute_evidence(m.orders[-1]["id"])
    assert ev["charge"]["method"] == "direct" and ev["order"]["method"] == "direct"
    assert ev["checkout"]["jwt"] and ev["receipt"]["claims"]["status"] == "Success"


def test_evidence_for_an_unknown_order_is_not_found(world):
    with pytest.raises(CheckoutError):
        world.merchants["northwind"].dispute_evidence("ord_ghost")


# ---- the audit trail -----------------------------------------------------------------------------


def test_the_audit_trail_records_each_step_in_order(world):
    m, oid = ap2_order(world)
    m.refund_order(oid)
    events = [e["event"] for e in m.audit]
    assert events == ["session_created", "checkout_signed", "order_paid", "order_refunded"]
    assert all(e["merchant"] == "northwind" and isinstance(e["ts"], int) for e in m.audit)
    paid = next(e for e in m.audit if e["event"] == "order_paid")
    assert paid["order"] == oid and paid["amount"] == 3499 and paid["method"] == "ap2"


def test_the_audit_trail_holds_no_card_number_or_key(world):
    m, oid = card_order(world)
    m.refund_order(oid)
    text = str(m.audit)
    assert "4242424242424242" not in text and "BEGIN" not in text and '"d"' not in text


# ---- over HTTP -----------------------------------------------------------------------------------


@pytest.fixture
def api(world):
    return TestClient(create_app(world.merchants["northwind"], {"t": "platform"}, operators={"op": "ops"}),
                      raise_server_exceptions=False)


def h(key="k", token="op"):
    return {"Authorization": f"Bearer {token}", "API-Version": ACP_API_VERSION, "Idempotency-Key": key}


def test_order_refund_and_evidence_over_http(world, api):
    m, oid = card_order(world)
    seen = api.get(f"/orders/{oid}", headers=h(token="t")).json()
    assert seen["type"] == "order" and seen["status"] == "created" and seen["adjustments"] == []
    part = api.post(f"/orders/{oid}/refund", json={"amount": 1000}, headers=h("r1"))
    assert part.status_code == 200 and part.json()["remaining"] == 2499
    over = api.post(f"/orders/{oid}/refund", json={"amount": 9999}, headers=h("r2"))
    assert over.status_code == 400 and over.json()["code"] == "invalid_refund"
    rest = api.post(f"/orders/{oid}/refund", json={}, headers=h("r3"))
    assert rest.json()["payment_status"] == "refunded"
    ev = api.get(f"/orders/{oid}/evidence", headers=h())
    assert ev.status_code == 200 and ev.json()["charge"]["refunded"] == 3499
    after = api.get(f"/orders/{oid}", headers=h(token="t")).json()
    assert [a["amount"] for a in after["adjustments"]] == [1000, 2499]
    assert after["totals"][-1] == {"type": "amount_refunded", "display_text": "Refunded", "amount": 3499}


def test_a_replayed_refund_request_refunds_once(world, api):
    m, oid = card_order(world)
    first = api.post(f"/orders/{oid}/refund", json={"amount": 1000}, headers=h("same"))
    again = api.post(f"/orders/{oid}/refund", json={"amount": 1000}, headers=h("same"))
    assert again.headers["Idempotent-Replayed"] == "true" and again.json() == first.json()
    assert world.processor.charges[0].refunded == 1000


@pytest.mark.parametrize("body", [{"amount": "10"}, {"amount": 1.5}, {"amount": True}])
def test_a_malformed_refund_amount_is_a_400(world, api, body):
    m, oid = card_order(world)
    r = api.post(f"/orders/{oid}/refund", json=body, headers=h(str(body)))
    assert r.status_code == 400 and world.processor.charges[0].refunded == 0


def test_an_agent_platform_cannot_move_money_or_goods(world, api):
    """Refunds, fulfillment, cancellation and evidence are the merchant's; a platform token gets 403."""
    m, oid = card_order(world)
    for path in ("refund", "fulfill", "deliver", "cancel"):
        r = api.post(f"/orders/{oid}/{path}", json={}, headers=h(path, token="t"))
        assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert api.get(f"/orders/{oid}/evidence", headers=h(token="t")).status_code == 403
    assert world.processor.charges[0].refunded == 0 and m.orders[0]["status"] == "created"


def test_order_endpoints_need_auth_and_know_unknown_orders(world, api):
    assert api.get("/orders/ord_x").status_code == 401
    assert api.get("/orders/ord_ghost", headers=h()).status_code == 404
    assert api.post("/orders/ord_ghost/refund", json={}, headers=h("g")).status_code == 404


# ---- refunds on Stripe (fake) --------------------------------------------------------------------


def fake_stripe(refund_fails=False):
    calls = {"intents": [], "refunds": []}

    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Intents:
        def create(self, params):
            calls["intents"].append(params)
            return Obj(id="pi_fake_1", status="succeeded")

    class Refunds:
        def create(self, params):
            calls["refunds"].append(params)
            if refund_fails:
                raise RuntimeError("charge already refunded req_zzz")
            return Obj(id=f"re_fake_{len(calls['refunds'])}")

    class V1:
        payment_intents, refunds = Intents(), Refunds()

    class StripeClient:
        def __init__(self, key):
            self.v1 = V1()

    mod = types.ModuleType("stripe")
    mod.StripeClient = StripeClient
    mod._calls = calls
    return mod


def test_a_refund_goes_back_on_the_stripe_rail(monkeypatch):
    mod = fake_stripe()
    monkeypatch.setitem(sys.modules, "stripe", mod)
    world = build(conftest.stores(), processor=StripeProcessor(api_key="sk_test_x"))
    m, oid = card_order(world)
    out = m.refund_order(oid, 1200)
    assert out["psp_ref"] == "re_fake_1"
    assert mod._calls["refunds"] == [{"payment_intent": "pi_fake_1", "amount": 1200}]
    assert m.dispute_evidence(oid)["refunds"] == [{"amount": 1200, "currency": "USD", "psp_ref": "re_fake_1"}]


def test_a_failed_stripe_refund_changes_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe(refund_fails=True))
    world = build(conftest.stores(), processor=StripeProcessor(api_key="sk_test_x"))
    m, oid = card_order(world)
    with pytest.raises(PaymentError, match="stripe refund failed") as exc:
        m.refund_order(oid)
    assert "req_zzz" not in str(exc.value)
    assert world.processor.charges[0].refunded == 0 and not world.processor.refunds
    assert m.orders[0]["payment_status"] == "paid"


@pytest.mark.skipif(not os.environ.get("STRIPE_TEST_KEY"), reason="no STRIPE_TEST_KEY set")
def test_live_stripe_refund():
    pytest.importorskip("stripe")
    world = build(conftest.stores(), processor=StripeProcessor.from_env())
    m, oid = card_order(world)
    out = m.refund_order(oid, 1000)
    assert out["psp_ref"].startswith("re_") and out["remaining"] == 2499


def test_the_processor_refunds_a_charge_once_even_called_directly(world, monkeypatch):
    """Defense in depth: two callers that skip the merchant (two workers, a support tool) still refund once."""
    card_order(world)
    charge = world.processor.charges[0]
    real = world.processor._refund_rail

    def slow(*a):
        import time
        time.sleep(0.02)
        return real(*a)

    monkeypatch.setattr(world.processor, "_refund_rail", slow)
    results, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(world.processor.refund(charge))
        except PaymentError as exc:
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert charge.refunded == 3499 and len(world.processor.refunds) == 1
