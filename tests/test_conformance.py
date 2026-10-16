"""AP2 v0.2 "MUST"s and the corner cases around them, one broken input per test.

Each test builds a valid purchase with ``Rig``, changes exactly one thing, and asserts which check
refuses it and with which AP2 error code. The first test in each group is the same purchase left
alone, so a failing refusal is never a failing setup.
"""

import base64
import json

import pytest
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant as MerchantRef
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument

from rig import Rig, first_word
from shipjack.keys import new_key
from shipjack.mandates import FULFILLMENT, TrustList, digest, jwt_payload
from shipjack.psp import PaymentError
from conftest import DOCK, HUB, HUB_PRO


# ---- the Checkout Mandate, merchant side ---------------------------------------------------------


def test_a_valid_chain_has_no_violations(world):
    r = Rig(world)
    closed, violations = r.verify_checkout(r.checkout_mandate())
    assert violations == [] and closed.checkout_hash == digest(r.checkout_jwt)


def test_wrong_audience_is_refused(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.checkout_mandate(aud="bluebird"))
    assert first_word(v) == "invalid_credential" and "aud mismatch" in v[0]


def test_wrong_nonce_is_refused(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.checkout_mandate(nonce="not-the-merchants-nonce"))
    assert first_word(v) == "invalid_credential" and "nonce mismatch" in v[0]


def test_an_open_mandate_alone_is_not_a_chain(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.deleg.open_checkout)
    assert first_word(v) == "invalid_credential"


def test_garbage_is_not_a_chain(world):
    r = Rig(world)
    for junk in ("", "~~", "a.b.c~~d.e.f", "not a token", "~" * 50):
        _, v = r.verify_checkout(junk)
        assert first_word(v) == "invalid_credential", junk


def test_a_root_signed_by_a_key_nobody_trusts_is_refused(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.checkout_mandate(), trust=TrustList([new_key("some-other-user")]))
    assert first_word(v) == "invalid_credential" and "untrusted key" in v[0]


def test_a_trust_list_matches_the_key_not_just_its_name(world):
    """A key that reuses the trusted key's kid must still fail the signature check."""
    r = Rig(world)
    impostor = new_key(world.surface.key.get("kid"))
    _, v = r.verify_checkout(r.checkout_mandate(), trust=TrustList([impostor]))
    assert first_word(v) == "invalid_credential"


def test_the_closed_mandate_must_be_signed_by_the_key_the_open_one_names(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.checkout_mandate(key=new_key("rogue-agent")))
    assert first_word(v) == "invalid_credential"


def test_an_expired_open_mandate_is_refused(world):
    r = Rig(world, lifetime=-1000)  # expired 1000 s ago; the SDK allows 300 s of clock skew
    _, v = r.verify_checkout(r.checkout_mandate())
    assert first_word(v) == "invalid_credential" and "exp" in v[0].lower()


def test_a_mandate_just_past_expiry_survives_within_the_clock_skew(world):
    r = Rig(world, lifetime=-60)
    _, v = r.verify_checkout(r.checkout_mandate())
    assert v == []  # documented: the SDK default tolerates 300 s of skew


def test_the_approval_does_not_cover_a_checkout_changed_afterwards(world):
    r = Rig(world)
    token = r.checkout_mandate()
    r.merchant.update_session(r.session_id, fulfillment_details={"address": DOCK})
    fresh = r.merchant.ap2_checkout(r.session_id)
    _, v = r.verify_checkout(token, checkout_jwt=fresh["checkout_jwt"], nonce=fresh["nonce"])
    assert first_word(v) == "invalid_credential" and "nonce mismatch" in v[0]  # refused before the cart is compared
    assert fresh["checkout_jwt"] != r.checkout_jwt and fresh["nonce"] != r.nonce


def test_a_tampered_checkout_is_not_the_merchants_checkout(world):
    r = Rig(world)
    head, body, sig = r.checkout_jwt.split(".")
    payload = jwt_payload(r.checkout_jwt)
    payload["totals"] = [{"type": "subtotal", "amount": 1}, {"type": "total", "amount": 1}]
    forged = ".".join((head, base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode(), sig))
    _, v = r.verify_checkout(r.checkout_mandate(jwt=forged))
    assert any("different checkout" in x for x in v)


def test_checkout_hash_must_be_the_hash_of_the_checkout(world):
    r = Rig(world)
    _, v = r.verify_checkout(r.checkout_mandate(hash_of="some other checkout"))
    assert any("checkout_hash does not match" in x for x in v)


def test_an_item_outside_the_open_mandates_line_items_is_refused(world):
    r = Rig(world, items=((HUB.id, 1), (HUB_PRO.id, 1)))
    _, v = r.verify_checkout(r.checkout_mandate())
    assert v and any("nw-hub-pro" in x or "line" in x.lower() or "quantity" in x.lower() for x in v)


def test_more_of_an_item_than_the_open_mandate_allows_is_refused(world):
    r = Rig(world, items=((HUB.id, 2),))
    _, v = r.verify_checkout(r.checkout_mandate())
    assert v


def test_an_empty_cart_does_not_satisfy_a_line_items_constraint(world):
    r = Rig(world)
    r.merchant.update_session(r.session_id, items=[])
    # an empty cart is not ready for payment, so the merchant will not even sign it
    with pytest.raises(Exception, match="not ready"):
        r.merchant.ap2_checkout(r.session_id)


def test_an_unknown_constraint_fails_closed(world):
    from typing import Literal

    from ap2.sdk.generated.open_checkout_mandate import OpenCheckoutMandate
    from pydantic import BaseModel

    from shipjack.keys import cnf
    from shipjack.mandates import present as close, sign_open
    from ap2.sdk.generated.checkout_mandate import CheckoutMandate

    class Mystery(BaseModel):
        type: Literal["example.mystery"] = "example.mystery"

    class OpenWithMystery(OpenCheckoutMandate):
        constraints: list[Mystery]

    r = Rig(world)
    open_token = sign_open(world.surface.key, OpenWithMystery(constraints=[Mystery()], cnf=cnf(r.agent_key)))
    token = close(r.agent_key, open_token, CheckoutMandate(checkout_jwt=r.checkout_jwt, checkout_hash=digest(r.checkout_jwt)),
                  aud=r.aud, nonce=r.nonce)
    _, v = r.verify_checkout(token)
    assert v == ["unresolved_constraint: example.mystery"]


def test_a_known_extension_the_merchant_does_not_support_fails_closed(world):
    r = Rig(world, extensions=True)
    _, v = r.verify_checkout(r.checkout_mandate(), supported=frozenset())
    assert v == [f"unresolved_constraint: {FULFILLMENT}"]


def test_the_fulfillment_constraint_refuses_a_checkout_that_names_no_destination(world):
    from shipjack.mandates import Fulfillment, check_fulfillment
    constraint = Fulfillment(ship_to=[DOCK], deliver_to=[])
    assert check_fulfillment(constraint, {"fulfillment_details": {}}) == ["fulfillment: checkout names no destination"]
    assert check_fulfillment(constraint, {}) == ["fulfillment: checkout names no destination"]


# ---- the nonce is single use at the merchant -----------------------------------------------------


def test_a_failed_attempt_burns_the_merchants_nonce(world):
    r = Rig(world)
    token = r.checkout_mandate()
    first = r.merchant.complete_session(r.session_id, {"handler_id": "ap2", "checkout_mandate": token,
                                                       "payment_token": "tok_does_not_exist"})
    assert first["error"]["code"] == "payment_declined"
    again = r.merchant.complete_session(r.session_id, {"handler_id": "ap2", "checkout_mandate": token,
                                                       "payment_token": "tok_does_not_exist"})
    assert again["error"]["code"] == "invalid_mandate"  # a new approval needs a new ap2_checkout
    assert not r.merchant.orders


def test_completing_without_asking_for_a_checkout_first_is_refused(world):
    r = Rig(world)
    fresh = r.merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": r.merchant.sessions[r.session_id]
                                                                           .fulfillment.address})
    result = r.merchant.complete_session(fresh["id"], {"handler_id": "ap2", "checkout_mandate": r.checkout_mandate(),
                                                       "payment_token": "x"})
    assert result["error"]["code"] == "invalid_mandate" and not r.merchant.orders


def test_a_mandate_for_one_session_does_not_work_on_another(world):
    r = Rig(world)
    other = r.merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": r.merchant.sessions[r.session_id]
                                                                           .fulfillment.address})
    r.merchant.ap2_checkout(other["id"])
    result = r.merchant.complete_session(other["id"], {"handler_id": "ap2", "checkout_mandate": r.checkout_mandate(),
                                                       "payment_token": "x"})
    assert result["error"]["code"] in ("invalid_credential", "invalid_mandate") and not r.merchant.orders


# ---- the Payment Mandate, credential-provider side -----------------------------------------------


def test_a_valid_payment_chain_has_no_violations(world):
    r = Rig(world)
    closed, v = r.verify_payment(r.payment_mandate())
    assert v == [] and closed.payment_amount.amount == r.total


def test_a_payment_presented_to_the_wrong_audience_is_refused(world):
    r = Rig(world)
    _, v = r.verify_payment(r.payment_mandate(aud="merchant"))
    assert first_word(v) == "invalid_credential"


def test_amount_boundaries(world):
    r = Rig(world)  # the user's limit is 4000 cents, inclusive
    ok = lambda cents: r.verify_payment(r.payment_mandate(r.payment(payment_amount=Amount(amount=cents, currency="USD"))))[1]
    assert ok(4000) == [] and ok(0) == []
    assert ok(4001) and any("exceeds" in x for x in ok(4001))
    assert ok(-1) and any("below" in x for x in ok(-1))


def test_a_different_currency_is_refused_even_for_a_small_amount(world):
    r = Rig(world)
    _, v = r.verify_payment(r.payment_mandate(r.payment(payment_amount=Amount(amount=1, currency="EUR"))))
    assert any("Currency mismatch" in x for x in v)


def test_a_payee_outside_the_allowed_list_is_refused(world):
    r = Rig(world)
    stranger = MerchantRef(id="dealsmart", name="DealsMart", website="https://dealsmart.example")
    _, v = r.verify_payment(r.payment_mandate(r.payment(payee=stranger)))
    assert any("not in allowed list" in x for x in v)


def test_the_payment_must_name_the_open_checkout_mandate_it_belongs_to(world):
    r = Rig(world)
    _, v = r.verify_payment(r.payment_mandate(), open_checkout_hash="hash-of-some-other-open-mandate")
    assert any("PaymentReference mismatch" in x for x in v)
    _, v = r.verify_payment(r.payment_mandate(), open_checkout_hash=None)
    assert any("open_checkout_hash is required" in x for x in v)


def test_a_payment_instrument_the_user_never_named_is_fine_when_unconstrained(world):
    """Documents the scope: the bench's open mandate does not constrain the instrument."""
    r = Rig(world)
    odd = PaymentInstrument(id="pi_other", type="card")
    _, v = r.verify_payment(r.payment_mandate(r.payment(payment_instrument=odd)))
    assert v == []


# ---- the credential provider and the processor ---------------------------------------------------


def test_a_credential_provider_nonce_is_single_use(world):
    r = Rig(world)
    cp = world.credentials
    nonce = cp.nonce()
    token = r.payment_mandate(nonce=nonce)
    cp.issue(token, nonce, r.deleg.open_checkout_hash)
    with pytest.raises(PaymentError) as again:
        cp.issue(token, nonce, r.deleg.open_checkout_hash)
    assert again.value.code == "invalid_credential"
    with pytest.raises(PaymentError) as unknown:
        cp.issue(r.payment_mandate(nonce="invented"), "invented", r.deleg.open_checkout_hash)
    assert unknown.value.code == "invalid_credential"


def test_a_credential_provider_can_refuse_to_reuse_an_open_payment_mandate(world):
    r = Rig(world)
    cp = world.credentials
    cp.reject_reuse = True
    outcomes = []
    for _ in range(2):
        nonce = cp.nonce()
        try:
            outcomes.append(cp.issue(r.payment_mandate(nonce=nonce), nonce, r.deleg.open_checkout_hash)[:4])
        except PaymentError as exc:
            outcomes.append(exc.code)
    assert outcomes == ["tok_", "invalid_mandate"]


def _token(r, world, **payment):
    cp = world.credentials
    nonce = cp.nonce()
    return cp.issue(r.payment_mandate(r.payment(**payment), nonce=nonce), nonce, r.deleg.open_checkout_hash)


def test_a_token_pays_once_and_only_for_its_checkout(world):
    r = Rig(world)
    token = _token(r, world)
    txn = digest(r.checkout_jwt)
    with pytest.raises(PaymentError, match="different checkout"):
        world.processor.charge_token(r.merchant.account, token, r.total, "USD", "some-other-transaction",
                                     r.deleg.open_checkout_hash)
    charge = world.processor.charge_token(r.merchant.account, token, r.total, "usd", txn, r.deleg.open_checkout_hash)
    assert charge.amount == r.total  # the currency compares without regard to case
    with pytest.raises(PaymentError, match="unknown or already used"):
        world.processor.charge_token(r.merchant.account, token, r.total, "USD", txn, r.deleg.open_checkout_hash)


def test_a_token_cannot_be_redeemed_for_a_different_amount(world):
    r = Rig(world)
    token = _token(r, world)
    txn = digest(r.checkout_jwt)
    for cents in (r.total - 1, r.total + 1, 0):
        with pytest.raises(PaymentError, match="approved"):
            world.processor.charge_token(r.merchant.account, token, cents, "USD", txn, r.deleg.open_checkout_hash)
    with pytest.raises(PaymentError, match="approved"):
        world.processor.charge_token(r.merchant.account, token, r.total, "EUR", txn, r.deleg.open_checkout_hash)
    assert not world.processor.charges


def test_a_token_is_bound_to_the_open_checkout_mandate(world):
    r = Rig(world)
    token = _token(r, world)
    with pytest.raises(PaymentError, match="open checkout mandate"):
        world.processor.charge_token(r.merchant.account, token, r.total, "USD", digest(r.checkout_jwt), "another-open-mandate")


def test_an_expired_token_is_refused(world):
    r = Rig(world)
    world.credentials.token_ttl = -1
    token = _token(r, world)
    with pytest.raises(PaymentError, match="expired"):
        world.processor.charge_token(r.merchant.account, token, r.total, "USD", digest(r.checkout_jwt),
                                     r.deleg.open_checkout_hash)


def test_a_token_pays_only_its_payee(world):
    r = Rig(world)
    token = _token(r, world)
    with pytest.raises(PaymentError, match="not account"):
        world.processor.charge_token(world.merchants["bluebird"].account, token, r.total, "USD",
                                     digest(r.checkout_jwt), r.deleg.open_checkout_hash)
