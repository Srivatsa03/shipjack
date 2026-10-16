"""The Stripe test-mode processor: key safety, the authorization guarantees, and a live smoke test.

Most tests use a fake ``stripe`` module so they are deterministic and need no key or network. The one
live test runs only when STRIPE_TEST_KEY is set and the stripe package is installed.
"""

import sys
import types

import pytest

from conftest import HOME, HUB, hub_intent
from shipjack.stripe_psp import TEST_PM_DECLINE, StripeProcessor
from shipjack.world import build

import conftest


def fake_stripe(outcome="ok"):
    """A stand-in stripe module: records PaymentIntent.create calls and returns a canned result."""
    calls, keys = [], []

    class Intent:
        def __init__(self, id, status):
            self.id, self.status = id, status

    class PaymentIntents:
        def create(self, params, options=None):
            calls.append(params)
            keys.append((options or {}).get("idempotency_key"))
            if outcome == "decline" or params.get("payment_method") == TEST_PM_DECLINE:
                raise RuntimeError("Your card was declined. request log req_abc123")
            if outcome == "pending":
                return Intent("pi_test_1", "requires_action")
            return Intent("pi_test_1", "succeeded")

    class StripeClient:
        def __init__(self, key):
            self.key = key
            self.payment_intents = PaymentIntents()

    mod = types.ModuleType("stripe")
    mod.StripeClient = StripeClient
    mod._calls, mod._keys = calls, keys
    return mod


@pytest.fixture
def with_fake_stripe(monkeypatch):
    def install(outcome="ok"):
        mod = fake_stripe(outcome)
        monkeypatch.setitem(sys.modules, "stripe", mod)
        return mod
    return install


# ---- key safety (no network, no key) -------------------------------------------------------------


@pytest.mark.parametrize("key", ["sk_live_123", "rk_live_123", "pk_test_123", "nonsense", "sk_test", ""])
def test_a_non_test_key_is_refused(key):
    with pytest.raises(ValueError):
        StripeProcessor(api_key=key)


@pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
def test_a_secret_or_restricted_test_key_is_accepted(key):
    assert StripeProcessor(api_key=key).api_key == key


def test_from_env_reads_the_test_key(monkeypatch):
    monkeypatch.setenv("STRIPE_TEST_KEY", "sk_test_abc")
    assert StripeProcessor.from_env().api_key == "sk_test_abc"
    monkeypatch.delenv("STRIPE_TEST_KEY")
    with pytest.raises(ValueError, match="STRIPE_TEST_KEY"):
        StripeProcessor.from_env()


# ---- the adapter logic against a fake Stripe -----------------------------------------------------


def world_on(processor):
    return build(conftest.stores(), processor=processor)


def test_a_card_purchase_settles_on_stripe_and_records_the_intent(with_fake_stripe):
    mod = with_fake_stripe("ok")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4242424242424242"}}})
    assert out["status"] == "completed"
    charge = world.processor.charges[0]
    assert charge.psp_ref == "pi_test_1" and charge.amount == 3499
    assert mod._calls[0]["amount"] == 3499 and mod._calls[0]["currency"] == "usd" and mod._calls[0]["confirm"] is True


def test_an_ap2_purchase_settles_on_stripe(with_fake_stripe):
    with_fake_stripe("ok")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    wallet = world.wallet("ap2", hub_intent(world))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = wallet.pay(m, s["id"])
    assert out["status"] == "paid" and world.processor.charges[0].psp_ref == "pi_test_1"


def test_a_stripe_decline_records_no_charge_and_does_not_complete(with_fake_stripe):
    with_fake_stripe("decline")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4242424242424242"}}})
    assert out["error"]["code"] == "payment_declined" and not world.processor.charges and not m.orders
    assert "req_abc123" not in out["error"]["message"] and "req_…" in out["error"]["message"]  # id scrubbed


def test_a_pending_intent_is_treated_as_not_paid(with_fake_stripe):
    with_fake_stripe("pending")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4242424242424242"}}})
    assert out["error"]["code"] == "payment_declined" and not world.processor.charges


def test_stripe_is_only_called_after_the_authorization_checks_pass(with_fake_stripe):
    mod = with_fake_stripe("ok")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    # a wrong card number is rejected by the processor's own check, before any Stripe call
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4000000000000002"}}})
    assert out["error"]["code"] == "payment_declined" and mod._calls == [] and not world.processor.charges


def test_an_ap2_token_for_the_wrong_payee_never_reaches_stripe(with_fake_stripe):
    mod = with_fake_stripe("ok")
    world = world_on(StripeProcessor(api_key="sk_test_x"))
    # an impostor store that copied northwind's id: the payee binding rejects it before Stripe
    from conftest import HUB as _HUB
    wallet = world.wallet("ap2", hub_intent(world))
    impostor = world.merchants["northwlnd"]
    s = impostor.create_session([{"id": _HUB.id}], fulfillment_details={"address": HOME})
    out = wallet.pay(impostor, s["id"])
    assert out["status"] == "declined" and mod._calls == [] and not world.processor.charges


def test_the_decline_test_card_declines(with_fake_stripe):
    with_fake_stripe("ok")  # the create() still declines because the pm is the decline card
    world = world_on(StripeProcessor(api_key="sk_test_x", payment_method=TEST_PM_DECLINE))
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4242424242424242"}}})
    assert out["error"]["code"] == "payment_declined" and not world.processor.charges


# ---- a live smoke test against Stripe's test API (skipped unless a key is set) --------------------


@pytest.mark.skipif(not __import__("os").environ.get("STRIPE_TEST_KEY"), reason="no STRIPE_TEST_KEY set")
def test_live_stripe_test_mode_settles_a_card_purchase():
    pytest.importorskip("stripe")
    world = world_on(StripeProcessor.from_env())
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    out = m.complete_session(s["id"], {"handler_id": "card", "instrument": {"type": "card",
                                                                           "credential": {"number": "4242424242424242"}}})
    assert out["status"] == "completed"
    assert world.processor.charges[0].psp_ref.startswith("pi_")


@pytest.mark.skipif(not __import__("os").environ.get("STRIPE_TEST_KEY"), reason="no STRIPE_TEST_KEY set")
def test_live_stripe_returns_the_first_payment_for_a_repeated_idempotency_key():
    """What makes a retry after a crash safe: Stripe answers the same key with the same PaymentIntent."""
    pytest.importorskip("stripe")
    import uuid
    processor = StripeProcessor.from_env()
    key = f"shipjack-test-{uuid.uuid4()}"
    first = processor._settle(1234, "USD", "cs_idempotency_check", "card", key)
    again = processor._settle(1234, "USD", "cs_idempotency_check", "card", key)
    other = processor._settle(1234, "USD", "cs_idempotency_check", "card", key + "-next")
    assert first.startswith("pi_") and again == first and other != first
