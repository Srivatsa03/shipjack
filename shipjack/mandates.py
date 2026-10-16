"""AP2 v0.2 mandate building and verification, on top of Google's AP2 SDK.

The SDK does the cryptography (SD-JWT, key-binding hops, constraint evaluation). This module adds
what a real deployment has to decide for itself:

- who is trusted to sign open mandates (a trust list keyed by ``kid``),
- that ``aud`` and ``nonce`` are always checked (the SDK only checks them when the caller passes them),
- that the presented ``checkout_jwt`` is the merchant's latest one, not just any signed checkout,
- an extension constraint for where the goods go, and failing closed on any constraint a verifier
  does not understand (AP2: "Any unknown Constraints MUST be treated as failing evaluation").
"""

from __future__ import annotations

import json
from typing import Literal

from ap2.sdk.constraints import MandateContext, check_checkout_constraints, check_payment_constraints
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import AllowedMerchants, LineItems, OpenCheckoutMandate
from ap2.sdk.generated.open_payment_mandate import OpenPaymentMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.checkout import Checkout
from ap2.sdk.mandate import MandateClient
from ap2.sdk.utils import b64url_decode, compute_sha256_b64url
from jwcrypto.jwk import JWK
from pydantic import BaseModel, Field

from .catalog import address_key, address_problems, email_key

FULFILLMENT = "io.github.srivatsa03.fulfillment"
SDK_CHECKOUT_TYPES = {"checkout.allowed_merchants", "checkout.line_items"}
SDK_PAYMENT_TYPES = {"payment.amount_range", "payment.allowed_payees", "payment.allowed_payment_instruments",
                     "payment.allowed_pisps", "payment.budget", "payment.agent_recurrence", "payment.reference",
                     "payment.execution_date"}

MAX_OPEN_LIFETIME = 24 * 3600  # AP2 recommends the shortest expiry that fits the task; this deployment caps it at a day

client = MandateClient()


def digest(text: str) -> str:
    """base64url SHA-256, the way AP2 computes ``checkout_hash`` and receipt references."""
    return compute_sha256_b64url(text)


def jwt_payload(token: str) -> dict:
    """Decode a compact JWT payload WITHOUT verifying it. Callers verify the signature separately."""
    return json.loads(b64url_decode(token.split(".")[1]))


class Fulfillment(BaseModel):
    """Extension constraint: the goods may only go to these destinations.

    AP2 v0.2 constrains which merchant, which items and how much, but nothing constrains where a
    physical order ships or which inbox receives a digital one. This closes that gap. Every
    destination the checkout names must be on the list, and a checkout that names none fails.
    """

    type: Literal["io.github.srivatsa03.fulfillment"] = FULFILLMENT
    ship_to: list[dict] = Field(default_factory=list)
    deliver_to: list[str] = Field(default_factory=list)


class OpenCheckoutMandateExt(OpenCheckoutMandate):
    constraints: list[AllowedMerchants | LineItems | Fulfillment] = Field(...)


def check_fulfillment(constraint: Fulfillment, checkout: dict) -> list[str]:
    details = checkout.get("fulfillment_details") or {}
    address, email = details.get("address"), details.get("email")
    if not address and not email:
        return ["fulfillment: checkout names no destination"]
    out = []
    if address:
        problems = address_problems(address)
        if problems:
            out.append(f"fulfillment: {problems[0]}")
        elif address_key(address) not in {address_key(a) for a in constraint.ship_to}:
            out.append(f"fulfillment: ship-to {address.get('line_one', '?')}, {address.get('city', '?')} is not allowed")
    if email:
        allowed = {k for k in map(email_key, constraint.deliver_to) if k}
        if not email_key(email) or email_key(email) not in allowed:
            out.append(f"fulfillment: delivery email {str(email)[:80]} is not allowed")
    return out


class TrustList:
    """Keys allowed to sign root (open) mandates, looked up by the ``kid`` in the token header."""

    def __init__(self, keys: list[JWK] | None = None):
        self.keys: dict[str, JWK] = {}
        for key in keys or []:
            self.add(key)

    def add(self, key: JWK) -> None:
        self.keys[key.get("kid")] = JWK.from_json(key.export_public())

    def __call__(self, token) -> JWK:
        kid = token.header.get("kid")
        if kid not in self.keys:
            raise ValueError(f"root mandate signed by an untrusted key: {kid!r}")
        return self.keys[kid]

    def verify_jws(self, token: str) -> dict:
        """Verify a compact JWS signed by a trusted key (by the ``kid`` in its header) and return the payload.

        Used for the user-signed direct approval in human-present mode. Raises ``ValueError`` if the
        header is malformed, the ``kid`` is not on the list, or the signature does not verify.
        """
        from ap2.sdk.jwt_helper import verify_jwt
        try:
            header = json.loads(b64url_decode(token.split(".")[0]))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"malformed JWS header: {exc}") from None
        kid = header.get("kid") if isinstance(header, dict) else None
        if kid not in self.keys:
            raise ValueError(f"signed by an untrusted key: {kid!r}")
        return verify_jwt(token, self.keys[kid])


def _split(constraints: list[dict], sdk_types: set[str], supported: set[str]) -> tuple[list, list, list[str]]:
    sdk, ext, unknown = [], [], []
    for c in constraints:
        kind = c.get("type")
        if kind in sdk_types:
            sdk.append(c)
        elif kind in supported:
            ext.append(c)
        else:
            unknown.append(kind)
    return sdk, ext, [f"unresolved_constraint: {k}" for k in unknown]


def _chain(token: str, trust: TrustList, aud: str, nonce: str) -> tuple[list[dict], str | None]:
    if "~~" not in token:
        return [], "invalid_credential: expected an open mandate chained with a closed one"
    try:
        payloads = client.verify(token=token, key_or_provider=trust, expected_aud=aud, expected_nonce=nonce)
    except Exception as exc:  # the SDK raises ValueError, InvalidSignature and jwcrypto errors
        return [], f"invalid_credential: {exc}"
    if len(payloads) != 2:
        return [], f"invalid_credential: expected 2 mandates in the chain, got {len(payloads)}"
    return payloads, None


def open_token_of(chain_token: str) -> str:
    return chain_token.split("~~", 1)[0] + "~"


def open_policy(open_raw: dict) -> list[str]:
    """Deployment policy on an open mandate: it must expire, and not later than a day after it was issued."""
    exp, iat = open_raw.get("exp"), open_raw.get("iat")
    if isinstance(exp, bool) or not isinstance(exp, int):
        return ["invalid_mandate: open mandate has no expiry"]
    if isinstance(iat, bool) or not isinstance(iat, int):
        return ["invalid_mandate: open mandate has no issue time"]
    if exp - iat > MAX_OPEN_LIFETIME:
        return [f"invalid_mandate: open mandate lives longer than {MAX_OPEN_LIFETIME // 3600} hours"]
    return []


def checkout_inconsistencies(checkout: dict) -> list[str]:
    """Does a merchant-signed checkout add up? A signature proves who said it, not that it is right.

    Each line's total is unit price x quantity, the subtotal is the sum of the lines, and with no
    shipping, tax, fee or discount entries the total is the subtotal. Totals are never negative.
    """
    out = []
    if not str(checkout.get("currency") or "").strip():
        out.append("checkout: no currency")
    lines = checkout.get("line_items")
    if not isinstance(lines, list) or not lines:
        return out + ["checkout: no line items"]
    line_sum = 0
    for li in lines:
        item, qty = (li.get("item") or {}), li.get("quantity")
        price = item.get("price")
        total = next((x.get("amount") for x in li.get("totals", []) if x.get("type") == "total"), None)
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            out.append(f"checkout: line {li.get('id')} has a bad quantity")
        elif isinstance(price, bool) or not isinstance(price, int) or total != price * qty:
            out.append(f"checkout: line {li.get('id')} total is not unit price x quantity")
        line_sum += total if isinstance(total, int) and not isinstance(total, bool) else 0
    entries = {x.get("type"): x.get("amount") for x in checkout.get("totals", []) if isinstance(x, dict)}
    total, subtotal = entries.get("total"), entries.get("subtotal")
    if not isinstance(total, int) or isinstance(total, bool):
        return out + ["checkout: no total"]
    if total < 0:
        out.append("checkout: negative total")
    if subtotal is not None and subtotal != line_sum:
        out.append("checkout: subtotal is not the sum of the lines")
    extras = set(entries) - {"subtotal", "total"}
    if not extras and total != line_sum:
        out.append("checkout: total does not add up to the lines")
    return out


def evaluate_checkout(open_payload: dict, checkout_jwt: str, supported: set[str] = frozenset()) -> list[str]:
    """Evaluate every constraint of an open Checkout Mandate against a checkout. Unknown types fail."""
    sdk, ext, violations = _split(open_payload.get("constraints", []), SDK_CHECKOUT_TYPES, supported)
    checkout = jwt_payload(checkout_jwt)
    violations += check_checkout_constraints(
        OpenCheckoutMandate.model_validate({**open_payload, "constraints": sdk}), Checkout.model_validate(checkout))
    for c in ext:
        violations += check_fulfillment(Fulfillment.model_validate(c), checkout)
    return violations


def evaluate_payment(open_payload: dict, closed: PaymentMandate, open_checkout_hash: str | None,
                     context: MandateContext | None = None) -> list[str]:
    """Evaluate every constraint of an open Payment Mandate against closed payment content."""
    sdk, _, violations = _split(open_payload.get("constraints", []), SDK_PAYMENT_TYPES, set())
    return violations + check_payment_constraints(
        OpenPaymentMandate.model_validate({**open_payload, "constraints": sdk}), closed,
        open_checkout_hash=open_checkout_hash, mandate_context=context or MandateContext())


def verify_checkout(token: str, trust: TrustList, *, aud: str, nonce: str, checkout_jwt: str,
                    supported: set[str] = frozenset()) -> tuple[CheckoutMandate | None, list[str]]:
    """Merchant-side check of a presented Checkout Mandate chain against its own latest checkout JWT."""
    payloads, error = _chain(token, trust, aud, nonce)
    if error:
        return None, [error]
    open_raw, closed_raw = payloads
    try:
        closed = CheckoutMandate.model_validate(closed_raw)
    except Exception as exc:
        return None, [f"invalid_mandate: {exc}"]
    violations = []
    if closed.checkout_jwt != checkout_jwt:
        violations.append("invalid_mandate: the mandate approves a different checkout than the current one")
    if closed.checkout_hash != digest(checkout_jwt):
        violations.append("invalid_mandate: checkout_hash does not match the current checkout")
    return closed, violations + open_policy(open_raw) + evaluate_checkout(open_raw, checkout_jwt, supported)


def verify_payment(token: str, trust: TrustList, *, aud: str, nonce: str, open_checkout_hash: str | None,
                   context: MandateContext | None = None) -> tuple[PaymentMandate | None, list[str]]:
    """Credential-provider-side check of a presented Payment Mandate chain."""
    payloads, error = _chain(token, trust, aud, nonce)
    if error:
        return None, [error]
    open_raw, closed_raw = payloads
    try:
        closed = PaymentMandate.model_validate(closed_raw)
    except Exception as exc:
        return None, [f"invalid_mandate: {exc}"]
    return closed, open_policy(open_raw) + evaluate_payment(open_raw, closed, open_checkout_hash, context)


def open_payload(token: str, key: JWK, model: type[BaseModel]) -> dict:
    """Verify a user-signed open mandate and return its content."""
    return client.verify(token=token, key_or_provider=key, payload_type=model).mandate_payload.model_dump(
        exclude_none=True)


def present(agent_key: JWK, open_token: str, closed: BaseModel, *, aud: str, nonce: str) -> str:
    """Close an open mandate: the agent key signs a key-binding hop over the closed mandate content."""
    return client.present(holder_key=agent_key, mandate_token=open_token, payloads=[closed], aud=aud, nonce=nonce)


def sign_open(user_key: JWK, mandate: BaseModel) -> str:
    return client.create(payloads=[mandate], issuer_key=user_key)


def error_code(violations: list[str]) -> str:
    """Map violations to AP2's action-authorization error codes."""
    codes = ("invalid_credential", "unresolved_constraint", "invalid_mandate", "mandates_not_supported")
    for v in violations:
        head = v.split(":", 1)[0].strip()
        if head in codes:
            return head
    return "invalid_mandate"
