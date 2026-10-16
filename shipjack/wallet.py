"""The deterministic half of the shopping agent: it holds the agent key and the user's mandates.

The LLM never sees a key, a card number or a mandate. It can only ask the wallet to pay for one
checkout session at one merchant. The wallet then builds and signs the closed mandates itself from
the merchant's signed checkout, the way AP2's implementation notes suggest ("deterministic code
verifies the closed Mandate being created before being signed").

Profiles:
- ``card``: the wallet hands the merchant a card number. Nothing limits the purchase.
- ``ap2``: human-not-present AP2 with every MUST we found, including the double-spend rule: no new
  closed mandate from an open one until the last presentation was rejected.
- ``ap2+ext``: ap2 plus a fulfillment constraint in the open mandate, and a guard that, before the
  agent key signs anything, checks the checkout JWT's signature against the merchant's published
  key and evaluates every constraint locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_payment_mandate import OpenPaymentMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant as MerchantRef
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.jwt_helper import verify_jwt
from jwcrypto.jwk import JWK

from .keys import new_key, public
from .mandates import (FULFILLMENT, OpenCheckoutMandateExt, digest, evaluate_checkout, evaluate_payment, jwt_payload,
                       open_payload, present)
from .merchant import CheckoutError, Merchant
from .psp import CP_AUDIENCE, Card, CredentialProvider, PaymentError
from .surface import Delegation, TrustedSurface

PROFILES = ("card", "ap2", "ap2+ext")


@dataclass
class Wallet:
    profile: str
    credentials: CredentialProvider
    surface: TrustedSurface | None = None
    delegation: Delegation | None = None
    directory: dict[str, JWK] = field(default_factory=dict)  # merchant id -> published checkout key
    agent_key: JWK = field(default_factory=lambda: new_key("shopping-agent-1"))
    card: Card = field(default_factory=Card)
    pending: str | None = None  # a closed mandate is out and no rejection has come back
    log: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}")

    @property
    def guarded(self) -> bool:
        return self.profile == "ap2+ext"

    def agent_public_key(self) -> JWK:
        return public(self.agent_key)

    def pay(self, merchant: Merchant, session_id: str) -> dict:
        try:
            result = self._card(merchant, session_id) if self.profile == "card" else self._ap2(merchant, session_id)
        except CheckoutError as exc:
            result = {"status": "declined", "code": exc.code, "message": str(exc), "stage": "checkout"}
        except PaymentError as exc:
            result = {"status": "declined", "code": exc.code, "message": str(exc),
                      "stage": getattr(exc, "stage", "wallet")}
        self.log.append({"merchant": merchant.id, "session": session_id, **result})
        return result

    def _card(self, merchant: Merchant, session_id: str) -> dict:
        data = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": self.card.number}}}
        return _outcome(merchant.complete_session(session_id, data))

    def _ap2(self, merchant: Merchant, session_id: str) -> dict:
        if self.pending:
            raise PaymentError("invalid_mandate", "the open mandate already approved a purchase; "
                                                  "AP2 forbids approving another one with it")
        d = self.delegation
        challenge = merchant.ap2_checkout(session_id)
        checkout_jwt = challenge["checkout_jwt"]
        checkout = jwt_payload(checkout_jwt)
        total = next(t["amount"] for t in checkout["totals"] if t["type"] == "total")
        payment = PaymentMandate(
            transaction_id=digest(checkout_jwt), payee=MerchantRef(**checkout["merchant"]),
            payment_amount=Amount(amount=total, currency=checkout["currency"]),
            payment_instrument=PaymentInstrument(id=self.card.instrument_id, type="card",
                                                 description=f"card ending {self.card.last4}"))
        if self.guarded:
            problems = self._guard(checkout_jwt, checkout, payment)
            if problems:
                raise PaymentError("invalid_mandate", "wallet refused to sign: " + "; ".join(problems))
        checkout_mandate = present(self.agent_key, d.open_checkout,
                                   CheckoutMandate(checkout_jwt=checkout_jwt, checkout_hash=digest(checkout_jwt)),
                                   aud=challenge["aud"], nonce=challenge["nonce"])
        self.pending = checkout_mandate
        try:
            nonce = self.credentials.nonce()
            try:
                token = self.credentials.issue(present(self.agent_key, d.open_payment, payment, aud=CP_AUDIENCE,
                                                       nonce=nonce), nonce, d.open_checkout_hash)
            except PaymentError as exc:
                exc.stage = "credential provider"
                raise
            outcome = _outcome(merchant.complete_session(
                session_id, {"handler_id": "ap2", "checkout_mandate": checkout_mandate, "payment_token": token,
                             "open_checkout_hash": d.open_checkout_hash}))
        except (CheckoutError, PaymentError):
            self.pending = None  # a rejection frees the open mandate for another try
            raise
        if outcome["status"] != "paid":
            self.pending = None
        return outcome

    def _guard(self, checkout_jwt: str, checkout: dict, payment: PaymentMandate) -> list[str]:
        merchant_id = checkout.get("merchant", {}).get("id")
        key = self.directory.get(merchant_id)
        problems = []
        if key is None:
            problems.append(f"no published key for merchant {merchant_id!r}")
        else:
            try:
                verify_jwt(checkout_jwt, key)
            except Exception:
                problems.append(f"checkout is not signed by {merchant_id}'s published key")
        ts_key = self.surface.verification_key()
        problems += evaluate_checkout(open_payload(self.delegation.open_checkout, ts_key, OpenCheckoutMandateExt),
                                      checkout_jwt, {FULFILLMENT})
        problems += evaluate_payment(open_payload(self.delegation.open_payment, ts_key, OpenPaymentMandate),
                                     payment, self.delegation.open_checkout_hash)
        return problems


def _outcome(session: dict) -> dict:
    if session.get("status") == "completed" and session.get("order"):
        order = session["order"]
        return {"status": "paid", "order_id": order["id"], "amount": order["amount"], "merchant": order["merchant"]}
    error = session.get("error") or {}
    code = error.get("code", "unknown")
    return {"status": "declined", "code": code, "message": error.get("message", ""),
            "stage": "processor" if code == "payment_declined" else "merchant"}
