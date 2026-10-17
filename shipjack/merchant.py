"""An agent-ready merchant: ACP checkout sessions, plus AP2 signed checkouts, mandate checks and receipts.

The session API follows the ACP Agentic Checkout spec (create, update, get, complete, cancel; integer
minor units; authoritative cart on every response). For AP2, the merchant signs the UCP Checkout
object as a JWT (ES256), hands out a fresh nonce for each approval, verifies the presented Checkout
Mandate against its latest checkout, and returns a signed Checkout Receipt.

``impostor_of`` and ``surcharge`` make a dishonest merchant for attack scenarios: one that claims
another merchant's identity in its checkouts, or charges more at completion than it showed.
"""

from __future__ import annotations

import functools
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ap2.sdk.jwt_helper import create_jwt
from ap2.sdk.mandate import MandateClient
from jwcrypto.jwk import JWK

from .catalog import Fulfillment, Product
from .keys import new_key, public
from .mandates import FULFILLMENT, TrustList, digest, error_code, verify_checkout
from .psp import PaymentError, Processor
from .store import MemoryRepo

ACP_API_VERSION = "2026-04-17"
MAX_QUANTITY = 1000  # per line; a store caps what one order can ask for
ADDRESS_KEYS = ("name", "line_one", "line_two", "city", "state", "country", "postal_code")  # ACP Address


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _locked_by(scope: str | None):
    """Run a public Merchant method as one transaction: one request changes a session at a time.

    ``scope`` says what the method's first argument names, and so what it locks: "session" (a checkout
    session id), "order" (an order id; an order shares its session's lock), "new" (it only creates rows),
    or None (the whole store). In memory that is the merchant's one lock; in SQLite one writer at a
    time; in Postgres a lock per session, so different shoppers' changes run in parallel. An error rolls
    the whole change back.
    """
    def decorate(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            with self.repo.transaction(keys=self._keys(scope, args[0] if args else None)):
                return method(self, *args, **kwargs)
        return wrapper
    return decorate


_locked = _locked_by(None)


def _reading(method):
    """Run a read-only Merchant method on one consistent snapshot."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.repo.transaction(write=False):
            return method(self, *args, **kwargs)
    return wrapper


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, param: str | None = None):
        super().__init__(message)
        self.code, self.param = code, param

    def __reduce__(self):  # picklable, so a refusal can cross a process boundary
        return (self.__class__, (self.code, str(self), self.param))

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
    attempts: int = 0  # failed payment attempts; part of the payment rail's idempotency key
    paying: dict | None = None  # the attempt now talking to the payment rail: its idempotency key and when


class Merchant:
    paying_timeout_s = 120.0  # an attempt marked this long with no charge on record died with its process

    def __init__(self, id: str, name: str, website: str, products: list[Product], processor: Processor,
                 trust: TrustList, *, supported: frozenset[str] = frozenset({FULFILLMENT}),
                 verifies_mandates: bool = True, impostor_of: dict | None = None, surcharge: int = 0,
                 repo=None):
        self.info = {"id": id, "name": name, "website": website}
        self.claimed = dict(impostor_of) if impostor_of else dict(self.info)
        self.repo = repo if repo is not None else MemoryRepo()
        # every server process and every restart must sign checkouts with the same key
        self.key: JWK = self.repo.signing_key(f"{id}-checkout-1", lambda: new_key(f"{id}-checkout-1"))
        self.products = {p.id: p for p in products}
        self.processor, self.trust, self.supported = processor, trust, supported
        self.verifies_mandates, self.surcharge = verifies_mandates, surcharge
        self.account = f"acct_{id}"
        processor.registered[self.account] = id
        self.listeners: list = []  # called as listener(event_type, acp_order) after each order change
        saved = self.repo.catalog()  # an imported catalog replaces the one this merchant was built with
        if saved:
            self.products = {d["id"]: _product(d) for d in saved}

    @_locked
    def replace_catalog(self, products: list[Product]) -> int:
        """Swap in a new catalog (an import). Orders keep the lines they were bought with."""
        if not products:
            raise CheckoutError("invalid", "a catalog needs at least one product")
        self.repo.save_catalog([{"id": p.id, "title": p.title, "price": p.price, "kind": p.kind,
                                 "description": p.description, "reviews": list(p.reviews)} for p in products])
        self.products = {p.id: p for p in products}
        self._log("catalog_imported", products=len(products))
        return len(products)

    # the state lives in the repository; these are read views of it
    @property
    def sessions(self):
        return self.repo.sessions

    @property
    def orders(self):
        return self.repo.orders

    @property
    def receipts(self):
        return self.repo.receipts

    @property
    def audit(self):
        """Append-only log of what happened, for reconciliation and disputes."""
        return self.repo.audit

    def _log(self, event: str, **fields) -> None:
        self.repo.log({"ts": int(time.time()), "merchant": self.id, "event": event, **fields})

    def _emit(self, event: str, order_id: str) -> None:
        """Tell listeners (e.g. the webhook outbox) about an order change, with the lock still held.

        Holding the lock keeps one order's events in the order the changes happened, and the snapshot
        is the full ACP Order at that moment, never a delta.
        """
        if self.listeners:
            snapshot = self.acp_order(order_id)
            for listener in list(self.listeners):
                listener(event, snapshot)

    @property
    def id(self) -> str:
        return self.info["id"]

    def _keys(self, scope: str | None, ident) -> tuple[str, ...] | None:
        """The lock keys for a change (see ``_locked_by``)."""
        if scope is None:
            return None
        if scope == "new" or not isinstance(ident, str):
            return ()
        return (self.repo.order_key(ident) if scope == "order" else self.repo.session_key(ident),)

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
    @_locked_by("new")
    def create_session(self, items: list[dict], fulfillment_details: dict | None = None,
                       buyer: dict | None = None) -> dict:
        details, buyer = self._details(fulfillment_details), self._buyer(buyer)
        cart = self._items(items)
        session = Session(f"cs_{self.id}_{self.repo.next_number()}", cart, details, buyer)
        self.repo.add_session(session)
        self._log("session_created", session=session.id, items=[list(i) for i in cart])
        return self._render(self._refresh(session))

    @_locked_by("session")
    def update_session(self, session_id: str, items: list[dict] | None = None,
                       fulfillment_details: dict | None = None, buyer: dict | None = None) -> dict:
        session = self._open(session_id)
        # validate everything first: a bad field must not leave the session half changed
        cart = self._items(items) if items is not None else None
        details = self._details(fulfillment_details) if fulfillment_details is not None else None
        buyer = self._buyer(buyer) if buyer is not None else None
        if cart is not None:
            session.items = cart
        if details is not None:
            session.fulfillment = details
        if buyer is not None:
            session.buyer = buyer
        return self._render(self._refresh(session))

    @_reading
    def get_session(self, session_id: str) -> dict:
        return self._render(self._session(session_id))

    @_locked_by("session")
    def cancel_session(self, session_id: str) -> dict:
        session = self._session(session_id)
        if session.status in ("completed", "canceled"):
            raise CheckoutError("invalid", f"session is already {session.status}")
        if session.paying:  # the rail may be charging it right now
            raise CheckoutError("payment_in_progress", "a payment for this session is in progress", "$.status")
        session.status, session.checkout_jwt, session.nonce = "canceled", None, None
        return self._render(session)

    def complete_session(self, session_id: str, payment_data: dict, *, record=None) -> dict:
        """Pay for a checkout session, in three steps so that no lock is held while the payment rail answers.

        1. Locked: check that the session is payable and the mandate or approval holds, then mark the
           attempt as paying, with its idempotency key. Another request for the session now gets
           ``payment_in_progress`` instead of a second charge.
        2. Unlocked: charge on the rail with that key. The processor's ledger records the charge.
        3. Locked: record the order (or the decline) and clear the mark.

        If the process dies between 1 and 3, the mark stays. The next request for the session, or
        ``reconcile_payments``, finishes the order from the ledger (or the rail), so it is charged once.

        ``record`` (the HTTP layer's idempotency record) rides in the same transactions: ``record.start()``
        first, in step 1 (its answer, if any, ends the request there), and ``record.finish(out)`` in the
        step that ends the request, so storing the answer costs no transaction of its own.
        """
        keys = (*self._keys("session", session_id), *(record.keys if record else ()))
        with self.repo.transaction(keys=keys):
            if record is not None:
                early = record.start()
                if early is not None:
                    return early
            started = self._start_payment(session_id, payment_data)
            if "attempt" not in started:  # refused before any money moved, or finished from the ledger
                return self._answer(record, started["out"])
        attempt = started["attempt"]
        try:
            charge = self._charge(attempt)
        except PaymentError as exc:
            with self.repo.transaction(keys=keys):
                return self._answer(record, self._declined(session_id, attempt["key"], exc))
        with self.repo.transaction(keys=keys):
            return self._answer(record, self._paid(session_id, attempt["key"], charge))

    @staticmethod
    def _answer(record, out: dict) -> dict:
        if record is not None:
            record.finish(out)
        return out

    def _start_payment(self, session_id: str, payment_data) -> dict:
        session = self._open(session_id, allow_paying=True)
        if session.status != "ready_for_payment":
            raise CheckoutError("invalid", "session is not ready for payment", "$.status")
        if session.paying:
            out = self._finish_attempt(session, self.paying_timeout_s)
            if out is not None:
                return {"out": out}
            if session.paying:
                raise CheckoutError("payment_in_progress", "a payment for this session is in progress; "
                                    "ask for the session again shortly", "$.status")
        if not isinstance(payment_data, dict):
            raise CheckoutError("invalid", "payment_data must be an object", "$.payment_data")
        handler = payment_data.get("handler_id")
        amount = self._total(session) + self.surcharge
        # the same attempt at the same amount gets the same key, so a retry after a crash cannot pay twice
        key = f"shipjack:{self.id}:{session.id}:{session.attempts}:{amount}"
        attempt = {"key": key, "amount": amount, "session": session.id, "method": handler}
        try:
            if handler == "card":
                instrument = payment_data.get("instrument")
                credential = instrument.get("credential") if isinstance(instrument, dict) else None
                number = credential.get("number") if isinstance(credential, dict) else None
                attempt["number"] = number if isinstance(number, str) else ""  # kept in memory, never stored
            elif handler == "ap2":
                attempt.update(self._authorize_ap2(session, payment_data))
            elif handler == "direct":
                attempt.update(self._authorize_direct(session, payment_data, amount))
            else:
                raise CheckoutError("invalid", "unsupported payment handler", "$.payment_data.handler_id")
        except PaymentError as exc:
            return {"out": self._decline(session, exc)}
        session.paying = {"key": key, "amount": amount, "method": handler, "since": time.time(),
                          "transaction_id": attempt.get("transaction_id", session.id),
                          "reference": attempt.get("reference", ""),
                          # what is being bought, as priced now: the order records this, whatever the
                          # catalog does while the rail answers
                          "lines": [{"product_id": pid, "title": self.products[pid].title,
                                     "unit_price": self.products[pid].price, "kind": self.products[pid].kind,
                                     "quantity": qty} for pid, qty in session.items]}
        return {"attempt": attempt}

    def _charge(self, attempt: dict):
        if attempt["method"] == "card":
            return self.processor.charge_card(self.account, attempt["number"], attempt["amount"], "USD",
                                              attempt["session"], idempotency_key=attempt["key"])
        return self.processor.charge_token(self.account, attempt["token"], attempt["amount"], "USD",
                                           attempt["transaction_id"], attempt.get("open_checkout_hash"),
                                           method=attempt["method"], idempotency_key=attempt["key"])

    def _declined(self, session_id: str, key: str, exc: PaymentError) -> dict:
        session = self._session(session_id)
        if not session.paying or session.paying["key"] != key:  # finished elsewhere in the meantime
            return self._render(session)
        session.paying = None
        return self._decline(session, exc)

    def _decline(self, session: Session, exc: PaymentError) -> dict:
        session.attempts += 1  # the next attempt gets a new key, so a retry after a decline can succeed
        session.messages = [{"type": "error", "code": "payment_declined", "content_type": "plain",
                             "content": str(exc)}]
        out = self._render(session)
        out["error"] = {"type": "processing_error", "code": exc.code, "message": str(exc)}
        return out

    def _paid(self, session_id: str, key: str, charge) -> dict:
        """Record the order for a charge that landed. Called with the session's lock held."""
        session = self._session(session_id)
        if not session.paying or session.paying["key"] != key:  # another request already recorded it
            return self._render(session)
        method = session.paying["method"]
        receipt = (self._signed_receipt(session.paying.get("reference", ""), "Success", order_id=f"ord_{session.id}")
                   if method in ("ap2", "direct") else None)
        order = {"id": f"ord_{session.id}", "checkout_session_id": session.id,
                 "permalink_url": f"{self.info['website']}/orders/{session.id}", "merchant": self.id,
                 "account": charge.account, "amount": charge.amount, "method": charge.method,
                 "payment_status": "paid", "refunded": 0, "status": "created", "created_at": _now(),
                 "items": list(session.items), "fulfillment_details": session.fulfillment.to_acp(),
                 # what was bought, as it was priced when the attempt began (not as the catalog is now)
                 "lines": session.paying["lines"], "fulfillments": [], "adjustments": []}
        self.repo.add_order(order, {"charge": charge, "checkout_jwt": session.checkout_jwt if method != "card"
                                    else None, "receipt": receipt,
                                    "mandate_reference": (receipt or {}).get("claims", {}).get("reference", "")})
        self._log("order_paid", order=order["id"], amount=charge.amount, method=charge.method, psp_ref=charge.psp_ref)
        session.order, session.status, session.messages, session.paying = order, "completed", [], None
        self._emit("order_create", order["id"])
        out = self._render(session)
        if receipt:
            out["ap2_receipt"] = receipt
        return out

    def _finish_attempt(self, session: Session, stale_after_s: float) -> dict | None:
        """An attempt still marked as paying. If its charge is on record, record the order (returns the
        completed session). If it is older than ``stale_after_s`` and the rail has nothing either, free
        the session for a new attempt (returns None, ``paying`` cleared). Otherwise it is still running
        (returns None, ``paying`` kept). Called with the session's lock held."""
        paying = session.paying
        charge = self.processor.find_charge(paying["key"])
        if charge is None and time.time() - paying["since"] >= stale_after_s:
            # a real rail may have charged before the process died: asking again with the same key
            # returns that charge instead of making a second one (a simulated rail has nothing to say)
            charge = self.processor.recover(self.account, paying["amount"], "USD", paying["transaction_id"],
                                            paying["method"], paying["key"])
            if charge is None:
                session.paying = None
                session.attempts += 1
                session.messages = [{"type": "error", "code": "payment_declined", "content_type": "plain",
                                     "content": "the last payment attempt did not complete; nothing was charged"}]
                self._log("payment_attempt_freed", session=session.id, key=paying["key"])
                return None
        if charge is None:
            return None
        self._log("payment_attempt_finished", session=session.id, key=paying["key"])
        return self._paid(session.id, paying["key"], charge)

    def reconcile_payments(self, stale_after_s: float | None = None) -> dict:
        """Finish or free payment attempts that a dead process left marked. Run it from a timer; every
        server process may run it at once (each session is re-checked under its lock)."""
        stale_after_s = self.paying_timeout_s if stale_after_s is None else stale_after_s
        counts = {"finished": 0, "freed": 0, "running": 0}
        for sid in self.repo.paying_sessions():
            with self.repo.transaction(keys=self._keys("session", sid)):
                session = self.repo.session(sid)
                if session is None or not session.paying or session.status != "ready_for_payment":
                    continue
                out = self._finish_attempt(session, stale_after_s)
                counts["finished" if out is not None else "freed" if session.paying is None else "running"] += 1
        return counts

    @_locked_by("order")
    def refund_order(self, order_id: str, amount: int | None = None) -> dict:
        """Return money for an order, in full or in part, on the same rail that charged it."""
        out = self._refund(order_id, amount)
        self._emit("order_update", order_id)
        return out

    def _refund(self, order_id: str, amount: int | None) -> dict:
        record, order = self._order(order_id)
        refund = self.processor.refund(record["charge"], amount)
        order["refunded"] = record["charge"].refunded
        order["payment_status"] = "refunded" if order["refunded"] >= order["amount"] else "partially_refunded"
        order["adjustments"].append({"id": f"adj_{len(order['adjustments']) + 1}", "type": "refund",
                                     "occurred_at": _now(), "status": "completed", "amount": refund.amount,
                                     "currency": refund.currency.lower()})
        self._log("order_refunded", order=order_id, amount=refund.amount, psp_ref=refund.psp_ref)
        return {"order_id": order_id, "refunded": refund.amount, "total_refunded": order["refunded"],
                "remaining": order["amount"] - order["refunded"], "payment_status": order["payment_status"],
                "psp_ref": refund.psp_ref}

    # ---- after the order: fulfillment ----------------------------------------------------------
    @_locked_by("order")
    def fulfill_order(self, order_id: str, *, carrier: str = "", tracking_number: str = "",
                      tracking_url: str = "") -> dict:
        """Hand the order over: physical lines ship (with tracking), digital lines are delivered by email."""
        _, order = self._order(order_id)
        if order["status"] != "created":
            raise CheckoutError("invalid", f"order is already {order['status']}", "$.status")
        for name, value in (("carrier", carrier), ("tracking_number", tracking_number), ("tracking_url", tracking_url)):
            if not isinstance(value, str) or len(value) > 500:
                raise CheckoutError("invalid", f"{name} must be text of at most 500 characters", f"$.{name}")
        if tracking_url and not tracking_url.startswith("https://"):
            raise CheckoutError("invalid", "tracking_url must be an https URL", "$.tracking_url")
        kinds = [line["kind"] for line in _lines(order, self.products)]
        lines = {kind: [{"id": f"li_{i + 1}", "quantity": qty} for i, (pid, qty) in enumerate(order["items"])
                        if kinds[i] == kind] for kind in ("physical", "digital")}
        now = _now()
        if lines["physical"]:
            shipment = {"id": f"ful_{len(order['fulfillments']) + 1}", "type": "shipping", "status": "shipped",
                        "line_items": lines["physical"],
                        "events": [{"id": "evt_1", "type": "shipped", "occurred_at": now}]}
            shipment.update({k: v for k, v in (("carrier", carrier), ("tracking_number", tracking_number),
                                               ("tracking_url", tracking_url)) if v})
            destination = _acp_address(order["fulfillment_details"].get("address"))
            if destination:
                shipment["destination"] = destination
            order["fulfillments"].append(shipment)
        if lines["digital"]:
            order["fulfillments"].append({"id": f"ful_{len(order['fulfillments']) + 1}", "type": "digital",
                                          "status": "delivered", "line_items": lines["digital"],
                                          "events": [{"id": "evt_1", "type": "delivered", "occurred_at": now}]})
        order["status"] = "shipped" if lines["physical"] else "completed"
        self._log("order_fulfilled", order=order_id, status=order["status"], carrier=carrier or None)
        self._emit("order_update", order_id)
        return self.acp_order(order_id)

    @_locked_by("order")
    def mark_delivered(self, order_id: str) -> dict:
        """The carrier delivered every shipment: the order is complete."""
        _, order = self._order(order_id)
        if order["status"] != "shipped":
            raise CheckoutError("invalid", f"order is {order['status']}, not shipped", "$.status")
        now = _now()
        for shipment in order["fulfillments"]:
            if shipment["type"] == "shipping" and shipment["status"] != "delivered":
                shipment["status"] = "delivered"
                shipment["events"].append({"id": f"evt_{len(shipment['events']) + 1}", "type": "delivered",
                                           "occurred_at": now})
        order["status"] = "completed"
        self._log("order_delivered", order=order_id)
        self._emit("order_update", order_id)
        return self.acp_order(order_id)

    @_locked_by("order")
    def cancel_order(self, order_id: str) -> dict:
        """Cancel a paid order before it ships, and return whatever has not been refunded yet."""
        _, order = self._order(order_id)
        if order["status"] != "created":
            raise CheckoutError("invalid", f"order is already {order['status']}; it can no longer be canceled",
                                "$.status")
        if order["refunded"] < order["amount"]:
            self._refund(order_id, None)
        order["status"] = "canceled"
        self._log("order_canceled", order=order_id)
        self._emit("order_update", order_id)
        return self.acp_order(order_id)

    @_reading
    def acp_order(self, order_id: str) -> dict:
        """The order as ACP 2026-04-17 describes it (the full Order object a webhook carries)."""
        _, order = self._order(order_id)
        shipped: dict[str, int] = {}
        for f in order["fulfillments"]:
            for ref in f["line_items"]:
                shipped[ref["id"]] = shipped.get(ref["id"], 0) + ref["quantity"]
        canceled = order["status"] == "canceled"
        line_items = []
        for i, line in enumerate(_lines(order, self.products)):
            qty = line["quantity"]
            current, done = (0 if canceled else qty), shipped.get(f"li_{i + 1}", 0)
            status = ("removed" if current == 0 else "fulfilled" if done >= current
                      else "partial" if done else "processing")
            line_items.append({"id": f"li_{i + 1}", "title": line["title"], "product_id": line["product_id"],
                               "quantity": {"ordered": qty, "current": current, "fulfilled": done},
                               "unit_price": line["unit_price"], "subtotal": line["unit_price"] * qty, "status": status})
        subtotal = sum(li["subtotal"] for li in line_items)
        totals = [{"type": "subtotal", "display_text": "Subtotal", "amount": subtotal}]
        if order["amount"] != subtotal:  # a surcharge added at completion is shown, never hidden
            totals.append({"type": "fee", "display_text": "Fee", "amount": order["amount"] - subtotal})
        totals.append({"type": "total", "display_text": "Total", "amount": order["amount"]})
        if order["refunded"]:
            totals.append({"type": "amount_refunded", "display_text": "Refunded", "amount": order["refunded"]})
        return {"type": "order", "id": order["id"], "checkout_session_id": order["checkout_session_id"],
                "permalink_url": order["permalink_url"], "status": order["status"], "line_items": line_items,
                "fulfillments": [dict(f) for f in order["fulfillments"]],
                "adjustments": [dict(a) for a in order["adjustments"]], "totals": totals}

    def _order(self, order_id: str) -> tuple[dict, dict]:
        record = self.repo.record(order_id) if isinstance(order_id, str) else None
        if record is None:
            raise CheckoutError("not_found", f"no order {order_id!r}")
        return record, self.repo.order(order_id)

    @_reading
    def dispute_evidence(self, order_id: str) -> dict:
        """The non-repudiable picture AP2 describes: the signed checkout, the mandate, the receipt, the money.

        It joins what the user approved (the merchant-signed checkout and the mandate/approval reference) to
        what actually moved (the charge and any refunds), so a dispute can be settled from stored evidence.
        """
        record, order = self._order(order_id)
        charge = record["charge"]
        jwt = record["checkout_jwt"]
        return {"order": dict(order), "merchant": dict(self.info),
                "checkout": {"jwt": jwt, "hash": digest(jwt) if jwt else None},
                "mandate_reference": record["mandate_reference"], "receipt": record["receipt"],
                "charge": {"amount": charge.amount, "currency": charge.currency, "method": charge.method,
                           "psp_ref": charge.psp_ref, "refunded": charge.refunded},
                "refunds": [{"amount": r.amount, "currency": r.currency, "psp_ref": r.psp_ref}
                            for r in self.processor.refunds if r.charge_reference == charge.reference]}

    # ---- AP2 -------------------------------------------------------------------------------------
    @_locked_by("session")
    def ap2_checkout(self, session_id: str) -> dict:
        """Sign the current cart as a UCP Checkout JWT and issue a single-use nonce for its approval."""
        session = self._open(session_id)
        if session.status != "ready_for_payment":
            raise CheckoutError("invalid", "session is not ready for payment", "$.status")
        session.checkout_jwt = create_jwt({"alg": "ES256", "typ": "JWT", "kid": self.key.get("kid")},
                                          self._ucp_checkout(session), self.key)
        session.nonce = secrets.token_urlsafe(16)
        self._log("checkout_signed", session=session.id, checkout_hash=digest(session.checkout_jwt))
        return {"checkout_jwt": session.checkout_jwt, "nonce": session.nonce, "aud": self.id}

    def _authorize_ap2(self, session: Session, payment_data: dict) -> dict:
        if not session.checkout_jwt or not session.nonce:
            raise PaymentError("invalid_mandate", "no signed checkout to approve: call ap2_checkout first")
        text = lambda v: v if isinstance(v, str) else ""  # noqa: E731  (wrong types are treated as missing)
        mandate, nonce = text(payment_data.get("checkout_mandate")), session.nonce
        session.nonce = None  # single use, success or not
        if self.verifies_mandates:
            _, violations = verify_checkout(mandate, self.trust, aud=self.id, nonce=nonce,
                                            checkout_jwt=session.checkout_jwt, supported=self.supported)
            if violations:
                self._receipt(mandate, "Error", error=violations[0].split(":")[0], detail="; ".join(violations))
                raise PaymentError(error_code(violations), "; ".join(violations))
        return {"token": text(payment_data.get("payment_token")), "transaction_id": digest(session.checkout_jwt),
                "open_checkout_hash": text(payment_data.get("open_checkout_hash")) or None,
                "reference": _reference(mandate)}

    def _authorize_direct(self, session: Session, payment_data: dict, amount: int) -> dict:
        """Human-present: the user signed this exact checkout on their trusted surface."""
        from .direct import verify_user_approval
        if not session.checkout_jwt or not session.nonce:
            raise PaymentError("invalid_mandate", "no signed checkout to approve: call ap2_checkout first")
        text = lambda v: v if isinstance(v, str) else ""  # noqa: E731
        approval, nonce = text(payment_data.get("approval")), session.nonce
        session.nonce = None  # single use, success or not
        claims, violations = verify_user_approval(approval, self.trust, aud=self.id, nonce=nonce,
                                                  checkout_jwt=session.checkout_jwt, amount=amount)
        if violations:
            self._receipt(approval, "Error", error=violations[0].split(":")[0], detail="; ".join(violations))
            raise PaymentError(error_code(violations), "; ".join(violations))
        return {"token": self.processor.credentials.authorize_direct(claims),
                "transaction_id": digest(session.checkout_jwt), "reference": _reference(approval)}

    def _receipt(self, mandate: str, status: str, **extra) -> dict:
        return self._signed_receipt(_reference(mandate), status, **extra)

    def _signed_receipt(self, reference: str, status: str, **extra) -> dict:
        claims = {"iss": self.info["website"], "iat": int(time.time()), "status": status, "reference": reference}
        claims.update({k: v for k, v in extra.items() if v})
        receipt = {"jwt": create_jwt({"alg": "ES256", "typ": "JWT", "kid": self.key.get("kid")}, claims, self.key),
                   "claims": claims}
        self.repo.add_receipt(receipt)
        return receipt

    # ---- internals -------------------------------------------------------------------------------
    def _items(self, items) -> list[tuple[str, int]]:
        """Validate the requested lines. The same product on two lines becomes one line with the quantities added."""
        if not isinstance(items, list):
            raise CheckoutError("invalid", "items must be an array", "$.items")
        merged: dict[str, int] = {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise CheckoutError("invalid", "each item must be an object", f"$.items[{i}]")
            pid = item.get("id")
            if not isinstance(pid, str) or pid not in self.products:
                raise CheckoutError("invalid", f"no product {str(pid)[:60]!r} at {self.info['name']}", f"$.items[{i}].id")
            qty = item.get("quantity", 1)
            if isinstance(qty, float) and qty.is_integer():
                qty = int(qty)  # JSON writes 1 as 1.0 in some clients
            if isinstance(qty, bool) or not isinstance(qty, int):
                raise CheckoutError("invalid", "quantity must be a whole number", f"$.items[{i}].quantity")
            if qty < 1:
                raise CheckoutError("invalid", "quantity must be at least 1", f"$.items[{i}].quantity")
            merged[pid] = merged.get(pid, 0) + qty
            if merged[pid] > MAX_QUANTITY:
                raise CheckoutError("invalid", f"at most {MAX_QUANTITY} of one item per order", f"$.items[{i}].quantity")
        return list(merged.items())

    @staticmethod
    def _details(data) -> Fulfillment:
        try:
            return Fulfillment.from_acp(data)
        except ValueError as exc:
            raise CheckoutError("invalid", str(exc), "$.fulfillment_details") from None

    @staticmethod
    def _buyer(data) -> dict | None:
        if data is not None and not isinstance(data, dict):
            raise CheckoutError("invalid", "buyer must be an object", "$.buyer")
        return data

    def _session(self, session_id: str) -> Session:
        session = self.repo.session(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise CheckoutError("not_found", f"no checkout session {session_id!r}")
        return session

    def _open(self, session_id: str, allow_paying: bool = False) -> Session:
        session = self._session(session_id)
        if session.status in ("completed", "canceled"):
            raise CheckoutError("invalid", f"session is already {session.status}", "$.status")
        if session.paying and not allow_paying:  # the cart, nonce and checkout must not change mid-payment
            raise CheckoutError("payment_in_progress", "a payment for this session is in progress", "$.status")
        return session

    def _total(self, session: Session) -> int:
        missing = [pid for pid, _ in session.items if pid not in self.products]
        if missing:
            raise CheckoutError("invalid", f"{missing[0]!r} is no longer sold; update the cart", "$.items")
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
        """A completed session shows what was sold (the order's lines); an open one, today's catalog."""
        sold = {line["product_id"]: line for line in (session.order or {}).get("lines", [])}
        out = []
        for i, (pid, qty) in enumerate(session.items):
            line = sold.get(pid)
            product = self.products.get(pid)
            title = line["title"] if line else product.title if product else f"{pid} (no longer sold)"
            price = line["unit_price"] if line else product.price if product else 0
            base = price * qty
            out.append({"id": f"li_{i + 1}", "item": {"id": pid, "quantity": qty}, "name": title,
                        "unit_amount": price, "base_amount": base, "discount": 0, "subtotal": base,
                        "tax": 0, "total": base})
        return out

    def _render(self, session: Session) -> dict:
        """The authoritative ACP cart state."""
        lines = self._line_items(session)
        total = sum(li["total"] for li in lines)
        return {"id": session.id, "status": session.status, "currency": "usd",
                "merchant": dict(self.claimed), "line_items": lines,
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


def _reference(mandate: str) -> str:
    """A receipt's reference: the hash of the closed mandate it answers ("" when there is none)."""
    try:
        return digest(MandateClient().get_closed_mandate_jwt(mandate))
    except Exception:  # noqa: BLE001 - a malformed or direct approval has no closed mandate to hash
        return ""


def _acp_address(address: dict | None) -> dict | None:
    """An order's shipping address as an ACP Address, or None when a required field is missing."""
    if not isinstance(address, dict):
        return None
    out = {k: address[k] for k in ADDRESS_KEYS if isinstance(address.get(k), str) and address[k]}
    return out if all(k in out for k in ("name", "line_one", "city", "state", "country", "postal_code")) else None


def _product(d: dict) -> Product:
    return Product(d["id"], d["title"], d["price"], kind=d.get("kind", "physical"), description=d.get("description", ""),
                   reviews=tuple(d.get("reviews", ())))


def _lines(order: dict, products: dict) -> list[dict]:
    """An order's lines as sold. Orders from before line snapshots fall back to the catalog."""
    if order.get("lines"):
        return order["lines"]
    return [{"product_id": pid, "title": products[pid].title, "unit_price": products[pid].price,
             "kind": products[pid].kind, "quantity": qty} for pid, qty in order["items"]]
