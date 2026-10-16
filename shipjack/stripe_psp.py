"""A processor that settles on Stripe in test mode, in place of the simulated one.

This is the FDE piece: the same merchant, wallet and mandate checks, but the money moves through a
real PSP. ``StripeProcessor`` keeps every authorization guarantee of ``Processor`` (the card or AP2
token is validated exactly as before) and adds a real Stripe PaymentIntent for each charge, confirmed
with a Stripe test payment method. A Stripe decline raises ``PaymentError`` before any charge is
recorded, so a declined rail leaves the order unpaid and the token unspent.

Nothing here touches real money. It uses a Stripe *test* secret key (``sk_test_...``) and Stripe's
test cards. Read the key from the environment; never put it in the repo or on a command line:

    export STRIPE_TEST_KEY="$(security find-generic-password -a "$USER" -s stripe-test-key -w)"

Enable it for the demo store with ``shipjack-serve --stripe`` or by passing a ``StripeProcessor`` to
``build``. Needs the optional dependency: ``pip install 'shipjack[stripe]'``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .psp import PaymentError, Processor

# Stripe's documented test payment methods: one that always succeeds, one that always declines.
TEST_PM_OK = "pm_card_visa"
TEST_PM_DECLINE = "pm_card_chargeDeclined"


@dataclass
class StripeProcessor(Processor):
    """A Processor whose ``_settle`` creates and confirms a Stripe PaymentIntent in test mode."""

    api_key: str = ""
    payment_method: str = TEST_PM_OK  # swap to TEST_PM_DECLINE to exercise a rail decline
    settlements: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not self.api_key:
            raise ValueError("StripeProcessor needs a Stripe test key (sk_test_... or rk_test_...)")
        # accept a secret or restricted TEST key; refuse anything live, so this can never touch real money
        if not (self.api_key.startswith("sk_test_") or self.api_key.startswith("rk_test_")):
            raise ValueError("refusing a key that is not a Stripe TEST key (sk_test_... or rk_test_...)")

    @classmethod
    def from_env(cls, var: str = "STRIPE_TEST_KEY", **kwargs) -> StripeProcessor:
        key = os.environ.get(var, "")
        if not key:
            raise ValueError(f"set {var} to a Stripe test secret key (sk_test_...)")
        return cls(api_key=key, **kwargs)

    def _settle(self, amount: int, currency: str, reference: str, method: str, idempotency_key: str = "") -> str:
        try:
            import stripe
        except ImportError:  # pragma: no cover - exercised only without the extra installed
            raise PaymentError("processing_error", "stripe is not installed: pip install 'shipjack[stripe]'") from None
        client = stripe.StripeClient(self.api_key)
        payment_intents = getattr(getattr(client, "v1", None), "payment_intents", None) or client.payment_intents
        try:
            intent = payment_intents.create(params={
                "amount": amount, "currency": currency.lower(), "payment_method": self.payment_method,
                "confirm": True, "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                "metadata": {"reference": reference[:400], "method": method}},
                **_options(idempotency_key))
        except Exception as exc:  # stripe.error.CardError and friends: a declined or invalid charge
            raise PaymentError("payment_declined", f"stripe declined: {_scrub(str(exc))}") from None
        if intent.status != "succeeded":
            raise PaymentError("payment_declined", f"stripe payment status {intent.status}")
        self.settlements.append({"id": intent.id, "amount": amount, "currency": currency, "reference": reference})
        return intent.id

    def _refund_rail(self, psp_ref: str, amount: int, currency: str, idempotency_key: str = "") -> str:
        if not psp_ref:
            return ""  # nothing settled on the rail (e.g. a simulated charge); nothing to refund there
        try:
            import stripe
        except ImportError:  # pragma: no cover
            raise PaymentError("processing_error", "stripe is not installed") from None
        client = stripe.StripeClient(self.api_key)
        refunds = getattr(getattr(client, "v1", None), "refunds", None) or client.refunds
        try:
            refund = refunds.create(params={"payment_intent": psp_ref, "amount": amount}, **_options(idempotency_key))
        except Exception as exc:
            raise PaymentError("processing_error", f"stripe refund failed: {_scrub(str(exc))}") from None
        return refund.id


def _options(idempotency_key: str) -> dict:
    """Stripe keeps an idempotency key for 24 hours: a retried request with the same key returns the
    first result instead of charging or refunding again."""
    return {"options": {"idempotency_key": idempotency_key}} if idempotency_key else {}


def _scrub(text: str) -> str:
    """Keep request ids and the key out of any error that reaches a caller or a log."""
    import re
    text = re.sub(r"sk_test_[A-Za-z0-9]+", "sk_test_…", text)
    return re.sub(r"req_[A-Za-z0-9]+", "req_…", text)[:200]
