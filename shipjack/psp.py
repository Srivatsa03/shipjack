"""The payment side: a credential provider that releases scoped tokens, and a processor with a ledger.

The processor ledger is the ground truth the bench scores against: it records which merchant
account was paid and how much, whatever any agent or merchant claims happened.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

from ap2.sdk.constraints import MandateContext

from .mandates import TrustList, error_code, verify_payment

CP_AUDIENCE = "credential-provider"


class PaymentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def __reduce__(self):  # picklable, so a refusal can cross a process boundary
        return (self.__class__, (self.code, str(self)))


@dataclass
class Charge:
    account: str  # the merchant account the processor paid out to
    amount: int
    currency: str
    method: str  # "card", "ap2" or "direct"
    reference: str  # checkout session id, or the AP2 transaction_id (the checkout hash)
    psp_ref: str = ""  # the payment-rail id (e.g. a Stripe PaymentIntent), when a real PSP settled it
    refunded: int = 0  # how much of this charge has been refunded
    id: str = field(default_factory=lambda: "ch_" + secrets.token_hex(8))
    idempotency_key: str = ""  # the payment attempt it settled; finds it again after a crash


@dataclass
class Refund:
    charge_reference: str
    amount: int
    currency: str
    psp_ref: str = ""  # the payment-rail refund id, when a real PSP refunded it
    charge_id: str = ""


@dataclass
class Card:
    number: str = "4242424242424242"
    last4: str = "4242"
    instrument_id: str = "pi_card_4242"


@dataclass
class _Scope:
    transaction_id: str
    amount: int
    currency: str
    payee_id: str
    expires: float
    open_checkout_hash: str | None
    used: bool = False
    in_flight: bool = False  # a charge holds it while the payment rail answers; nobody else may use it


@dataclass
class Processor:
    """Moves the money. Each merchant has an account; ``registered`` is who that account really is."""

    charges: list[Charge] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    registered: dict[str, str] = field(default_factory=dict)  # account -> merchant id it was onboarded as
    credentials: CredentialProvider | None = None
    binds_payee: bool = True  # pay out only if the token's payee is the account's onboarded identity
    settle_delay_s: float = 0.0  # simulated payment-rail latency (a real PSP call takes 100s of ms)
    # guards tokens and refunds. Never held during a charge's rail call or ledger write: a token is reserved
    # under it, the rail is called and the ledger written without it (a database ledger takes its own
    # lock, and taking it while holding this one could deadlock with a transaction that waits for this)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def refund(self, charge: Charge, amount: int | None = None) -> Refund:
        """Return money for a charge, in full or in part. A merchant cannot refund more than it took."""
        with self._lock:
            amount = charge.amount - charge.refunded if amount is None else amount
            if amount <= 0:
                raise PaymentError("invalid_refund", "refund amount must be positive")
            if charge.refunded + amount > charge.amount:
                raise PaymentError("invalid_refund",
                                   f"refund {amount} over the {charge.amount - charge.refunded} still refundable")
            # what was refunded before this one identifies the step, so a retry after a crash refunds once
            key = f"shipjack-refund:{charge.id}:{charge.refunded}:{amount}"
            psp_ref = self._refund_rail(charge.psp_ref, amount, charge.currency, key)
            charge.refunded += amount
            refund = Refund(charge.reference, amount, charge.currency, psp_ref, charge.id)
            self.refunds.append(refund)
            return refund

    def find_charge(self, idempotency_key: str) -> Charge | None:
        """The charge a payment attempt made, by its idempotency key (None if it never landed)."""
        find = getattr(self.charges, "find", None)
        if find is not None:
            return find(idempotency_key)
        return next((c for c in list(self.charges) if c.idempotency_key == idempotency_key), None)

    def recover(self, account: str, amount: int, currency: str, reference: str, method: str,
                idempotency_key: str) -> Charge | None:
        """After a crash: did the rail charge this attempt although nothing was recorded? A simulated rail
        keeps no state outside the ledger, so no. A real PSP asks again with the same key (see
        StripeProcessor), which returns the first charge, or makes the one the user approved, once."""
        return None

    def _refund_rail(self, psp_ref: str, amount: int, currency: str, idempotency_key: str = "") -> str:
        """Return money on the payment rail. The base processor is simulated; a real PSP overrides this."""
        return ""

    def charge_card(self, account: str, card_number: str, amount: int, currency: str, reference: str,
                    idempotency_key: str = "") -> Charge:
        """A raw card number authorizes any amount for anyone who holds it. That is the baseline.

        Nothing is locked while the rail answers: the caller (the merchant) holds the checkout, and two
        charges for different checkouts have nothing to share."""
        return self._charge_card(account, card_number, amount, currency, reference, idempotency_key)

    def _charge_card(self, account: str, card_number: str, amount: int, currency: str, reference: str,
                     idempotency_key: str = "") -> Charge:
        if not self.credentials or card_number != self.credentials.card.number:
            raise PaymentError("payment_declined", "card number not recognised")
        psp_ref = self._settle(amount, currency, reference, "card", idempotency_key)
        charge = Charge(account, amount, currency, "card", reference, psp_ref, idempotency_key=idempotency_key)
        self.charges.append(charge)
        return charge

    def charge_token(self, account: str, token: str, amount: int, currency: str, transaction_id: str,
                     open_checkout_hash: str | None = None, method: str = "ap2", idempotency_key: str = "") -> Charge:
        """An AP2 token pays one checkout, for its approved amount, to its approved payee, once.

        ``method`` labels the charge: "ap2" for a mandate chain, "direct" for a user-signed approval.
        """
        return self._charge_token(account, token, amount, currency, transaction_id, open_checkout_hash, method,
                                  idempotency_key)

    def _charge_token(self, account: str, token: str, amount: int, currency: str, transaction_id: str,
                      open_checkout_hash: str | None = None, method: str = "ap2",
                      idempotency_key: str = "") -> Charge:
        with self._lock:  # checking the token and reserving it are one step
            scope = self._reserve(account, token, amount, currency, transaction_id, open_checkout_hash)
        try:
            psp_ref = self._settle(amount, currency, transaction_id, method, idempotency_key)
        except BaseException:
            with self._lock:
                scope.in_flight = False  # a decline (or a crash in the call) must not spend the token
            raise
        charge = Charge(account, amount, currency, method, transaction_id, psp_ref, idempotency_key=idempotency_key)
        with self._lock:
            scope.used, scope.in_flight = True, False
        self.charges.append(charge)
        return charge

    def _reserve(self, account: str, token: str, amount: int, currency: str, transaction_id: str,
                 open_checkout_hash: str | None) -> _Scope:
        scope = self.credentials.redeem(token) if self.credentials else None
        if scope is None:
            raise PaymentError("payment_declined", "unknown or already used payment token")
        if time.time() > scope.expires:
            raise PaymentError("payment_declined", "payment token expired")
        if transaction_id != scope.transaction_id:
            raise PaymentError("payment_declined", "token was approved for a different checkout")
        if amount != scope.amount or currency.upper() != scope.currency.upper():
            raise PaymentError("payment_declined", f"token approved {scope.amount} {scope.currency}, "
                                                   f"charge asked for {amount} {currency}")
        if scope.open_checkout_hash and open_checkout_hash != scope.open_checkout_hash:
            raise PaymentError("payment_declined", "token is bound to a different open checkout mandate")
        if self.binds_payee and self.registered.get(account) != scope.payee_id:
            raise PaymentError("payment_declined", f"token pays {scope.payee_id}, not account {account}")
        scope.in_flight = True
        return scope

    def _settle(self, amount: int, currency: str, reference: str, method: str, idempotency_key: str = "") -> str:
        """Move the money on the payment rail. The base processor is simulated and settles instantly.

        A real PSP (see shipjack/stripe_psp.py) overrides this to charge, and raises PaymentError on a
        decline. It runs after every authorization check and before the charge is recorded, so a
        decline leaves no charge and does not spend the token. ``idempotency_key`` is the same for a
        retry of the same payment attempt, so a rail that honours it charges once.
        """
        if self.settle_delay_s:
            time.sleep(self.settle_delay_s)
        return ""


@dataclass
class CredentialProvider:
    """Holds the user's card. Releases a single-use token only for a verified, in-constraint Payment Mandate."""

    trust: TrustList
    card: Card = field(default_factory=Card)
    token_ttl: float = 900
    reject_reuse: bool = False  # AP2: credential providers MAY reject overlapping mandates
    _nonces: set[str] = field(default_factory=set)
    _tokens: dict[str, _Scope] = field(default_factory=dict)
    _usage: dict[str, MandateContext] = field(default_factory=dict)  # open payment mandate -> spend so far
    log: list[dict] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def nonce(self) -> str:
        value = secrets.token_urlsafe(16)
        with self._lock:
            self._nonces.add(value)
        return value

    def issue(self, payment_mandate: str, nonce: str, open_checkout_hash: str | None) -> str:
        with self._lock:  # a nonce is spent and a token is minted as one step
            return self._issue(payment_mandate, nonce, open_checkout_hash)

    def _issue(self, payment_mandate: str, nonce: str, open_checkout_hash: str | None) -> str:
        if nonce not in self._nonces:
            raise PaymentError("invalid_credential", "unknown or already used nonce")
        self._nonces.discard(nonce)
        open_key = payment_mandate.split("~~", 1)[0]
        context = self._usage.setdefault(open_key, MandateContext())
        if self.reject_reuse and context.total_uses:
            raise PaymentError("invalid_mandate", "this open payment mandate was already used")
        closed, violations = verify_payment(payment_mandate, self.trust, aud=CP_AUDIENCE, nonce=nonce,
                                            open_checkout_hash=open_checkout_hash, context=context)
        self.log.append({"event": "issue", "violations": violations})
        if violations:
            raise PaymentError(error_code(violations), "; ".join(violations))
        token = "tok_" + secrets.token_urlsafe(12)
        self._tokens[token] = _Scope(closed.transaction_id, closed.payment_amount.amount,
                                     closed.payment_amount.currency, closed.payee.id,
                                     time.time() + self.token_ttl, open_checkout_hash)
        context.total_amount += closed.payment_amount.amount
        context.total_uses += 1
        context.last_used_date = time.time()
        return token

    def authorize_direct(self, approval: dict) -> str:
        """Mint a single-use token from a user-signed direct approval the merchant has already verified.

        Human-present mode: the user authorized this exact payee and amount on their trusted surface,
        so there is no payment mandate chain and no open mandate. The processor still binds the token
        to the payee, amount and checkout, and spends it once.
        """
        with self._lock:
            token = "tok_" + secrets.token_urlsafe(12)
            self._tokens[token] = _Scope(approval["checkout_hash"], approval["amount"], approval["currency"],
                                         approval["payee_id"], time.time() + self.token_ttl, None)
            return token

    def redeem(self, token: str) -> _Scope | None:
        scope = self._tokens.get(token)
        if scope is None or scope.used or scope.in_flight:
            return None
        return scope
