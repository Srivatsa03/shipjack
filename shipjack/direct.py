"""Human-present (Direct) mode: the user approves and signs each purchase on a trusted surface.

In this mode there are no open mandates. The agent assembles a cart, the merchant signs a checkout,
and the agent brings the exact checkout to the user's trusted surface. The surface renders an
*approval screen*, the user reads it and signs (or declines), and the user-signed approval is what
the merchant and processor verify. AP2: "the signature on the closed Mandates is validated as coming
from a User directly".

What the screen shows is a policy choice. AP2's example approval screen (agent_authorization.md)
shows the merchant, the amount and a table of items, and no delivery destination. ``Screen.SPEC``
matches that. ``Screen.WITH_DESTINATION`` adds the ship-to address and delivery email. A user can
only check what the screen shows, so the screen policy decides whether a careful user catches a
redirected order. That is the human-present face of the delivery gap in AP2 issue #381.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass

from ap2.sdk.jwt_helper import create_jwt
from jwcrypto.jwk import JWK

from .catalog import address_key, email_key
from .keys import public
from .mandates import digest, jwt_payload
from .surface import Intent


class Screen(enum.Enum):
    """How much of the purchase the trusted surface shows the user before they sign."""

    SPEC = "spec"                         # merchant, amount, item table — matches AP2's example screen
    WITH_DESTINATION = "with_destination" # the above plus the ship-to address and delivery email


@dataclass
class ApprovalRequest:
    """What the agent asks the user to approve: the exact, merchant-signed purchase."""

    merchant_id: str
    merchant_name: str
    currency: str
    amount: int
    line_items: list[dict]          # [{title, quantity, unit_amount, total}]
    ship_to: dict | None
    email: str | None
    checkout_hash: str
    nonce: str


def request_from_checkout(checkout_jwt: str, merchant_info: dict, nonce: str) -> ApprovalRequest:
    c = jwt_payload(checkout_jwt)
    total = next(t["amount"] for t in c["totals"] if t["type"] == "total")
    details = c.get("fulfillment_details") or {}
    lines = [{"title": li["item"].get("title", li["item"]["id"]), "quantity": li["quantity"],
              "unit_amount": li["item"].get("price"), "total": next(t["amount"] for t in li["totals"]
                                                                    if t["type"] == "total")}
             for li in c["line_items"]]
    return ApprovalRequest(c["merchant"]["id"], c["merchant"].get("name", c["merchant"]["id"]), c["currency"],
                           total, lines, details.get("address"), details.get("email"), digest(checkout_jwt), nonce)


def render(request: ApprovalRequest, screen: Screen) -> dict:
    """The approval screen the user actually sees. Only these fields reach the user's eyes."""
    view = {"merchant_name": request.merchant_name, "amount": request.amount, "currency": request.currency,
            "items": [{"name": li["title"], "qty": li["quantity"], "price": li["unit_amount"], "total": li["total"]}
                      for li in request.line_items]}
    if screen is Screen.WITH_DESTINATION:
        view["ship_to"] = request.ship_to
        view["deliver_to"] = request.email
    return view


@dataclass
class User:
    """A careful human at the trusted surface. They check the screen against what they asked for.

    They can only judge what the screen shows. If the screen omits the destination, they cannot tell
    an order that ships to them from one that ships to an attacker, so they approve either.
    """

    intent: Intent
    key: JWK
    sees_through_screens: bool = False  # a test-only switch; a real user never sees past the rendered screen

    def review(self, request: ApprovalRequest, screen: Screen) -> tuple[bool, str]:
        view = render(request, screen)
        if self.intent.merchants and request.merchant_id not in {m["id"] for m in self.intent.merchants}:
            return False, f"merchant {request.merchant_id} is not one I chose"
        if self.intent.items is not None:
            # the titles the user was offered, each mapped to the product behind it
            offered = {title: pid for choices, _ in self.intent.items for pid, title in choices}
            lines = [([pid for pid, _ in choices], qty) for choices, qty in self.intent.items]
            used = [False] * len(lines)
            for li in view["items"]:
                pid = offered.get(li["name"])
                match = next((i for i, (accept, qty) in enumerate(lines)
                              if not used[i] and pid in accept and li["qty"] == qty), None)
                if match is None:
                    return False, "the items are not what I asked for"
                used[match] = True
            if not all(used):
                return False, "the items are not what I asked for"
        if view["amount"] > self.intent.max_total:
            return False, f"total {view['amount']} is over my budget {self.intent.max_total}"
        source = view if (screen is Screen.WITH_DESTINATION or self.sees_through_screens) else None
        if source is not None:
            ship = request.ship_to if self.sees_through_screens else source.get("ship_to")
            mail = request.email if self.sees_through_screens else source.get("deliver_to")
            if ship and self.intent.ship_to and address_key(ship) != address_key(self.intent.ship_to):
                return False, f"it ships to {ship.get('line_one')}, not my address"
            if mail and self.intent.email and email_key(mail) != email_key(self.intent.email):
                return False, f"it emails {mail}, not me"
        return True, "matches what I asked for"


class DirectSurface:
    """The user's trusted surface in human-present mode: render, get consent, sign with the user key."""

    def __init__(self, user: User, *, lifetime: int = 900):
        self.user = user
        self.lifetime = lifetime

    def verification_key(self) -> JWK:
        return public(self.user.key)

    def approve(self, request: ApprovalRequest, screen: Screen) -> tuple[str | None, str]:
        ok, reason = self.user.review(request, screen)
        if not ok:
            return None, reason
        now = int(time.time())
        claims = {"typ": "ap2/direct-approval", "checkout_hash": request.checkout_hash, "aud": request.merchant_id,
                  "nonce": request.nonce, "amount": request.amount, "currency": request.currency,
                  "payee_id": request.merchant_id, "iat": now, "exp": now + self.lifetime}
        approval = create_jwt({"alg": "ES256", "typ": "JWT", "kid": self.user.key.get("kid")}, claims, self.user.key)
        return approval, reason


def new_user(intent: Intent, name: str = "sam") -> User:
    from .keys import new_key
    return User(intent=intent, key=new_key(f"{name}-user-credential-1"))


def purchase_direct(world, store: str, session_id: str, user: User, screen: Screen) -> dict:
    """One human-present purchase: sign a checkout, show the user a screen, and charge only if they sign.

    The user's credential is trusted by the ecosystem (added to the trust list), as if enrolled in advance.
    """
    merchant = world.merchants[store]
    world.credentials.trust.add(public(user.key))
    challenge = merchant.ap2_checkout(session_id)
    request = request_from_checkout(challenge["checkout_jwt"], merchant.info, challenge["nonce"])
    approval, reason = DirectSurface(user).approve(request, screen)
    if approval is None:
        return {"status": "declined", "stage": "user", "message": reason}
    result = merchant.complete_session(session_id, {"handler_id": "direct", "approval": approval})
    if result.get("status") == "completed" and result.get("order"):
        order = result["order"]
        return {"status": "paid", "order_id": order["id"], "amount": order["amount"], "merchant": order["merchant"]}
    error = result.get("error") or {}
    return {"status": "declined", "stage": "merchant", "code": error.get("code", "unknown"),
            "message": error.get("message", "")}


def verify_user_approval(approval_jwt: str, trust, *, aud: str, nonce: str, checkout_jwt: str, amount: int,
                         now: int | None = None, skew: int = 300) -> tuple[dict | None, list[str]]:
    """Merchant side of a direct purchase: the user-signed approval must match this exact checkout."""
    try:
        claims = trust.verify_jws(approval_jwt)
    except Exception as exc:  # untrusted signer or bad signature
        return None, [f"invalid_credential: {exc}"]
    now = now if now is not None else int(time.time())
    v = []
    if claims.get("typ") != "ap2/direct-approval":
        v.append("invalid_mandate: not a direct approval")
    if claims.get("aud") != aud or claims.get("payee_id") != aud:
        v.append("invalid_mandate: approval is for a different merchant")
    if claims.get("nonce") != nonce:
        v.append("invalid_credential: nonce mismatch")
    if claims.get("checkout_hash") != digest(checkout_jwt):
        v.append("invalid_mandate: approval is for a different checkout")
    if claims.get("amount") != amount or claims.get("currency") != "USD":
        v.append("invalid_mandate: approved amount does not match the checkout")
    exp = claims.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool) or now > exp + skew:
        v.append("invalid_credential: approval has expired")
    return (None if v else claims), v
