"""The Trusted Surface: deterministic UI that gets the user's consent and signs open mandates.

In the human-not-present flow the user approves constraints once, at the start; after that the
agent buys on its own inside them. Here the constraints come straight from the task's ``Intent``,
not from the LLM. That is the best case for AP2: anything that still goes wrong, goes wrong with
the user's constraints exactly as intended.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ap2.sdk.generated.open_checkout_mandate import AllowedMerchants, LineItemRequirements, LineItems
from ap2.sdk.generated.open_checkout_mandate import Item as MandateItem
from ap2.sdk.generated.open_payment_mandate import AllowedPayees, AmountRange, OpenPaymentMandate, PaymentReference
from ap2.sdk.generated.types.merchant import Merchant as MerchantRef
from jwcrypto.jwk import JWK

from .keys import cnf, new_key, public
from .mandates import Fulfillment, OpenCheckoutMandateExt, digest, sign_open


@dataclass
class Intent:
    """What the user actually asked for."""

    task: str  # the request the agent is given, in plain words
    max_total: int  # cents
    merchants: list[dict] | None = None  # allowed merchants ({id, name, website}); None = any merchant
    items: list[tuple[list[tuple[str, str]], int]] | None = None  # [([(product id, title), ...], qty)]; None = any
    ship_to: dict | None = None  # ACP address the goods must go to
    email: str | None = None  # where digital goods must be delivered


@dataclass
class Delegation:
    open_checkout: str
    open_payment: str
    open_checkout_hash: str
    intent: Intent
    extensions: bool


class TrustedSurface:
    def __init__(self, user: str = "user", lifetime: int = 3600):
        self.key: JWK = new_key(f"{user}-trusted-surface-1")
        self.lifetime = lifetime

    def verification_key(self) -> JWK:
        return public(self.key)

    def delegate(self, intent: Intent, agent_key: JWK, *, extensions: bool = False) -> Delegation:
        now = int(time.time())
        constraints: list = []
        if intent.merchants:
            constraints.append(AllowedMerchants(allowed=[MerchantRef(**m) for m in intent.merchants]))
        if intent.items:
            constraints.append(LineItems(items=[
                LineItemRequirements(id=f"line_{i + 1}", quantity=qty,
                                     acceptable_items=[MandateItem(id=pid, title=title) for pid, title in choices])
                for i, (choices, qty) in enumerate(intent.items)]))
        if extensions:
            constraints.append(Fulfillment(ship_to=[intent.ship_to] if intent.ship_to else [],
                                           deliver_to=[intent.email] if intent.email else []))
        open_checkout = sign_open(self.key, OpenCheckoutMandateExt(
            constraints=constraints, cnf=cnf(agent_key), iat=now, exp=now + self.lifetime))
        checkout_hash = digest(open_checkout)
        payment_constraints: list = [AmountRange(currency="USD", max=intent.max_total, min=0),
                                     PaymentReference(conditional_transaction_id=checkout_hash)]
        if intent.merchants:
            payment_constraints.append(AllowedPayees(allowed=[MerchantRef(**m) for m in intent.merchants]))
        open_payment = sign_open(self.key, OpenPaymentMandate(
            constraints=payment_constraints, cnf=cnf(agent_key), iat=now, exp=now + self.lifetime))
        return Delegation(open_checkout, open_payment, checkout_hash, intent, extensions)
