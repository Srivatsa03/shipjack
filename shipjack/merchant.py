"""An agent-ready merchant: ACP checkout sessions, plus AP2 signed checkouts, mandate checks and receipts.

The session API follows the ACP Agentic Checkout spec (create, update, get, complete, cancel; integer
minor units; authoritative cart on every response). For AP2, the merchant signs the UCP Checkout
object as a JWT (ES256), hands out a fresh nonce for each approval, verifies the presented Checkout
Mandate against its latest checkout, and returns a signed Checkout Receipt.

``impostor_of`` and ``surcharge`` make a dishonest merchant for attack scenarios: one that claims
another merchant's identity in its checkouts, or charges more at completion than it showed.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from ap2.sdk.jwt_helper import create_jwt
from ap2.sdk.mandate import MandateClient
from jwcrypto.jwk import JWK

from .catalog import Fulfillment, Product
from .keys import new_key, public
from .mandates import FULFILLMENT, TrustList, digest, error_code, verify_checkout
from .psp import PaymentError, Processor

ACP_API_VERSION = "2026-04-17"


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, param: str | None = None):
        super().__init__(message)
        self.code, self.param = code, param

    def body(self) -> dict:
        out = {"type": "invalid_request", "code": self.code, "message": str(self)}
        if self.param:
            out["param"] = self.param
        return out


@dataclass
class Session:
    id: str
    items: list[tuple[str, int]]
    fulfillment: Fulfillment
    buyer: dict | None = None
    status: str = "not_ready_for_payment"
    checkout_jwt: str | None = None
    nonce: str | None = None
    order: dict | None = None
    messages: list[dict] = field(default_factory=list)


class Merchant:
    def __init__(self, id: str, name: str, website: str, products: list[Product], processor: Processor,
                 trust: TrustList, *, supported: frozenset[str] = frozenset({FULFILLMENT}),
                 verifies_mandates: bool = True, impostor_of: dict | None = None, surcharge: int = 0):
        self.info = {"id": id, "name": name, "website": website}
        self.claimed = dict(impostor_of) if impostor_of else dict(self.info)
        self.key: JWK = new_key(f"{id}-checkout-1")
        self.products = {p.id: p for p in products}
        self.processor, self.trust, self.supported = processor, trust, supported
        self.verifies_mandates, self.surcharge = verifies_mandates, surcharge
        self.account = f"acct_{id}"
        processor.registered[self.account] = id
        self.sessions: dict[str, Session] = {}
        self.orders: list[dict] = []
        self.receipts: list[dict] = []
        self._count = 0

    @property
    def id(self) -> str:
        return self.info["id"]

    def verification_key(self) -> JWK:
        return public(self.key)

    # ---- catalog ---------------------------------------------------------------------------------
    def search(self, query: str = "") -> list[dict]:
        words = [w for w in query.lower().split() if w]
        hits = [p for p in self.products.values()
                if not words or any(w in f"{p.title} {p.description}".lower() for w in words)]
        return [{"id": p.id, "title": p.title, "price": p.price, "currency": "usd", "kind": p.kind} for p in hits]

    def product(self, product_id: str) -> dict:
        if product_id not in self.products:
            raise CheckoutError("not_found", f"no product {product_id!r} at {self.info['name']}")
        return self.products[product_id].page()

    # ---- ACP checkout sessions -------------------------------------------------------------------
    def create_session(self, items: list[dict], fulfillment_details: dict | None = None,
                       buyer: dict | None = None) -> dict:
        self._count += 1
        session = Session(f"cs_{self.id}_{self._count}", [], Fulfillment.from_acp(fulfillment_details), buyer)
        session.items = self._items(items)
        self.sessions[session.id] = session
        return self._render(self._refresh(session))

    def update_session(self, session_id: str, items: list[dict] | None = None,
                       fulfillment_details: dict | None = None, buyer: dict | None = None) -> dict:
        session = self._open(session_id)
        if items is not None:
            session.items = self._items(items)
        if fulfillment_details is not None:
            session.fulfillment = Fulfillment.from_acp(fulfillment_details)
        if buyer is not None:
            session.buyer = buyer
        return self._render(self._refresh(session))

    def get_session(self, session_id: str) -> dict:
        return self._render(self._session(session_id))

    def cancel_session(self, session_id: str) -> dict:
        session = self._session(session_id)
        if session.status in ("completed", "canceled"):
            raise CheckoutError("invalid", f"session is already {session.status}")
        session.status, session.checkout_jwt, session.nonce = "canceled", None, None
        return self._render(session)

    def complete_session(self, session_id: str, payment_data: dict) -> dict:
        session = self._open(session_id)
        if session.status != "ready_for_payment":
            raise CheckoutError("invalid", "session is not ready for payment", "$.status")
        handler = payment_data.get("handler_id")
        amount = self._total(session) + self.surcharge
        try:
            if handler == "card":
                number = (payment_data.get("instrument") or {}).get("credential", {}).get("number", "")
                charge = self.processor.charge_card(self.account, number, amount, "USD", session.id)
                receipt = None
            elif handler == "ap2":
                charge, receipt = self._complete_ap2(session, payment_data, amount)
            else:
                raise CheckoutError("invalid", f"unsupported payment handler {handler!r}", "$.payment_data.handler_id")
        except PaymentError as exc:
            session.messages = [{"type": "error", "code": "payment_declined", "content_type": "plain",
                                 "content": str(exc)}]
            out = self._render(session)
            out["error"] = {"type": "processing_error", "code": exc.code, "message": str(exc)}
            return out
        order = {"id": f"ord_{session.id}", "checkout_session_id": session.id,
                 "permalink_url": f"{self.info['website']}/orders/{session.id}", "merchant": self.id,
                 "account": charge.account, "amount": charge.amount, "method": charge.method,
                 "items": list(session.items), "fulfillment_details": session.fulfillment.to_acp()}
        self.orders.append(order)
        session.order, session.status, session.messages = order, "completed", []
        out = self._render(session)
        if receipt:
            out["ap2_receipt"] = receipt
        return out

    # ---- AP2 -------------------------------------------------------------------------------------
    def ap2_checkout(self, session_id: str) -> dict:
        """Sign the current cart as a UCP Checkout JWT and issue a single-use nonce for its approval."""
        session = self._open(session_id)
        if session.status != "ready_for_payment":
            raise CheckoutError("invalid", "session is not ready for payment", "$.status")
        session.checkout_jwt = create_jwt({"alg": "ES256", "typ": "JWT", "kid": self.key.get("kid")},
                                          self._ucp_checkout(session), self.key)
        session.nonce = secrets.token_urlsafe(16)
        return {"checkout_jwt": session.checkout_jwt, "nonce": session.nonce, "aud": self.id}

    def _complete_ap2(self, session: Session, payment_data: dict, amount: int):
        if not session.checkout_jwt or not session.nonce:
            raise PaymentError("invalid_mandate", "no signed checkout to approve: call ap2_checkout first")
        mandate, nonce = payment_data.get("checkout_mandate", ""), session.nonce
        session.nonce = None  # single use, success or not
        if self.verifies_mandates:
            _, violations = verify_checkout(mandate, self.trust, aud=self.id, nonce=nonce,
                                            checkout_jwt=session.checkout_jwt, supported=self.supported)
            if violations:
                self._receipt(mandate, "Error", error=violations[0].split(":")[0], detail="; ".join(violations))
                raise PaymentError(error_code(violations), "; ".join(violations))
        charge = self.processor.charge_token(self.account, payment_data.get("payment_token", ""), amount, "USD",
                                             digest(session.checkout_jwt), payment_data.get("open_checkout_hash"))
        return charge, self._receipt(mandate, "Success", order_id=f"ord_{session.id}")

    def _receipt(self, mandate: str, status: str, **extra) -> dict:
        try:
            reference = digest(MandateClient().get_closed_mandate_jwt(mandate))
        except Exception:
            reference = ""
        claims = {"iss": self.info["website"], "iat": int(time.time()), "status": status, "reference": reference}
        claims.update({k: v for k, v in extra.items() if v})
        receipt = {"jwt": create_jwt({"alg": "ES256", "typ": "JWT", "kid": self.key.get("kid")}, claims, self.key),
                   "claims": claims}
        self.receipts.append(receipt)
        return receipt

    # ---- internals -------------------------------------------------------------------------------
    def _items(self, items: list[dict]) -> list[tuple[str, int]]:
        out = []
        for i, item in enumerate(items or []):
            pid, qty = item.get("id"), int(item.get("quantity", 1))
            if pid not in self.products:
                raise CheckoutError("invalid", f"no product {pid!r} at {self.info['name']}", f"$.items[{i}].id")
            if qty < 1:
                raise CheckoutError("invalid", "quantity must be at least 1", f"$.items[{i}].quantity")
            out.append((pid, qty))
        return out

    def _session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            raise CheckoutError("not_found", f"no checkout session {session_id!r}")
        return self.sessions[session_id]

    def _open(self, session_id: str) -> Session:
        session = self._session(session_id)
        if session.status in ("completed", "canceled"):
            raise CheckoutError("invalid", f"session is already {session.status}", "$.status")
        return session

    def _total(self, session: Session) -> int:
        return sum(self.products[pid].price * qty for pid, qty in session.items)

    def _refresh(self, session: Session) -> Session:
        """Any change invalidates the signed checkout, so an old approval cannot cover a new cart."""
        session.checkout_jwt, session.nonce = None, None
        kinds = {self.products[pid].kind for pid, _ in session.items}
        missing = []
        if not session.items:
            missing.append(("$.items", "add at least one item"))
        if "physical" in kinds and not session.fulfillment.address:
            missing.append(("$.fulfillment_details.address", "a shipping address is required"))
        if "digital" in kinds and not session.fulfillment.email:
            missing.append(("$.fulfillment_details.email", "a delivery email is required"))
        session.messages = [{"type": "error", "code": "missing", "param": p, "content_type": "plain", "content": c}
                            for p, c in missing]
        session.status = "not_ready_for_payment" if missing else "ready_for_payment"
        return session

    def _line_items(self, session: Session) -> list[dict]:
        out = []
        for i, (pid, qty) in enumerate(session.items):
            product, base = self.products[pid], self.products[pid].price * qty
            out.append({"id": f"li_{i + 1}", "item": {"id": pid, "quantity": qty}, "name": product.title,
                        "unit_amount": product.price, "base_amount": base, "discount": 0, "subtotal": base,
                        "tax": 0, "total": base})
        return out

    def _render(self, session: Session) -> dict:
        """The authoritative ACP cart state."""
        total = self._total(session)
        return {"id": session.id, "status": session.status, "currency": "usd",
                "merchant": dict(self.claimed), "line_items": self._line_items(session),
                "fulfillment_details": session.fulfillment.to_acp(),
                "totals": [{"type": "subtotal", "display_text": "Subtotal", "amount": total},
                           {"type": "fulfillment", "display_text": "Shipping", "amount": 0},
                           {"type": "tax", "display_text": "Tax", "amount": 0},
                           {"type": "total", "display_text": "Total", "amount": total}],
                "messages": list(session.messages),
                "links": [{"type": "terms_of_use", "url": f"{self.info['website']}/terms"}],
                **({"order": session.order} if session.order else {})}

    def _ucp_checkout(self, session: Session) -> dict:
        """UCP Checkout object (the AP2 SDK's model), carrying ACP fulfillment details as an extension field."""
        total = self._total(session)
        line_items = [{"id": f"li_{i + 1}", "item": {"id": pid, "title": self.products[pid].title,
                                                     "price": self.products[pid].price},
                       "quantity": qty, "totals": [{"type": "subtotal", "amount": self.products[pid].price * qty},
                                                   {"type": "total", "amount": self.products[pid].price * qty}]}
                      for i, (pid, qty) in enumerate(session.items)]
        return {"id": session.id, "merchant": dict(self.claimed), "line_items": line_items,
                "status": "ready_for_complete", "currency": "USD",
                "totals": [{"type": "subtotal", "amount": total}, {"type": "total", "amount": total}],
                "links": [{"type": "terms_of_service", "url": f"{self.info['website']}/terms"}],
                "fulfillment_details": session.fulfillment.to_acp(),
                "iat": int(time.time()), "jti": secrets.token_urlsafe(12)}

