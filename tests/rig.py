"""A purchase set up piece by piece, so a test can break exactly one thing.

``Rig`` plays every role by hand: the user's trusted surface signs open mandates, the agent key
closes them, the merchant signs a checkout, and the verifiers check the result. Tests change one
input (a nonce, an audience, an expiry, a byte of the checkout) and assert which check refuses.
"""

from __future__ import annotations

from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant as MerchantRef
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument

from conftest import HOME, HUB, hub_intent
from shipjack.keys import new_key
from shipjack.mandates import FULFILLMENT, TrustList, digest, present, verify_checkout, verify_payment
from shipjack.psp import CP_AUDIENCE


class Rig:
    def __init__(self, world, *, store="northwind", lifetime=3600, address=HOME, items=((HUB.id, 1),),
                 intent=None, extensions=False):
        self.world = world
        world.surface.lifetime = lifetime
        self.agent_key = new_key("agent-under-test")
        self.intent = (intent or hub_intent)(world)
        self.deleg = world.surface.delegate(self.intent, self.agent_key, extensions=extensions)
        self.merchant = world.merchants[store]
        session = self.merchant.create_session([{"id": p, "quantity": q} for p, q in items],
                                               fulfillment_details={"address": address})
        self.session_id = session["id"]
        self.total = next(t["amount"] for t in session["totals"] if t["type"] == "total")
        challenge = self.merchant.ap2_checkout(self.session_id)
        self.checkout_jwt, self.nonce, self.aud = challenge["checkout_jwt"], challenge["nonce"], challenge["aud"]
        self.trust = TrustList([world.surface.verification_key()])

    # ---- the checkout side ----------------------------------------------------------------------
    def checkout_mandate(self, *, key=None, jwt=None, hash_of=None, aud=None, nonce=None) -> str:
        jwt = jwt or self.checkout_jwt
        return present(key or self.agent_key, self.deleg.open_checkout,
                       CheckoutMandate(checkout_jwt=jwt, checkout_hash=digest(hash_of or jwt)),
                       aud=aud or self.aud, nonce=nonce or self.nonce)

    def verify_checkout(self, token, **kw):
        return verify_checkout(token, kw.get("trust", self.trust), aud=kw.get("aud", self.aud),
                               nonce=kw.get("nonce", self.nonce), checkout_jwt=kw.get("checkout_jwt", self.checkout_jwt),
                               supported=kw.get("supported", {FULFILLMENT}))

    # ---- the payment side -----------------------------------------------------------------------
    def payment(self, **overrides) -> PaymentMandate:
        fields = dict(transaction_id=digest(self.checkout_jwt), payee=MerchantRef(**self.merchant.info),
                      payment_amount=Amount(amount=self.total, currency="USD"),
                      payment_instrument=PaymentInstrument(id="pi_card_4242", type="card"))
        fields.update(overrides)
        return PaymentMandate(**fields)

    def payment_mandate(self, payment=None, *, key=None, aud=CP_AUDIENCE, nonce="cp-nonce") -> str:
        return present(key or self.agent_key, self.deleg.open_payment, payment or self.payment(), aud=aud, nonce=nonce)

    def verify_payment(self, token, **kw):
        return verify_payment(token, kw.get("trust", self.trust), aud=kw.get("aud", CP_AUDIENCE),
                              nonce=kw.get("nonce", "cp-nonce"),
                              open_checkout_hash=kw.get("open_checkout_hash", self.deleg.open_checkout_hash),
                              context=kw.get("context"))


def first_word(violations: list[str]) -> str:
    """The AP2 error code a verifier answers with."""
    return violations[0].split(":")[0] if violations else ""
