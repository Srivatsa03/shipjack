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


@dataclass
class Charge:
    account: str  # the merchant account the processor paid out to
    amount: int
    currency: str
    method: str  # "card" or "ap2"
    reference: str  # checkout session id, or the AP2 transaction_id (the checkout hash)


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


@dataclass
class Processor:
    """Moves the money. Each merchant has an account; ``registered`` is who that account really is."""

    charges: list[Charge] = field(default_factory=list)
    registered: dict[str, str] = field(default_factory=dict)  # account -> merchant id it was onboarded as
    credentials: CredentialProvider | None = None
    binds_payee: bool = True  # pay out only if the token's payee is the account's onboarded identity
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)  # "unused" and "mark used" are one step

    def charge_card(self, account: str, card_number: str, amount: int, currency: str, reference: str) -> Charge:
        """A raw card number authorizes any amount for anyone who holds it. That is the baseline."""
        with self._lock:
            return self._charge_card(account, card_number, amount, currency, reference)

    def _charge_card(self, account: str, card_number: str, amount: int, currency: str, reference: str) -> Charge:
        if not self.credentials or card_number != self.credentials.card.number:
            raise PaymentError("payment_declined", "card number not recognised")
        charge = Charge(account, amount, currency, "card", reference)
        self.charges.append(charge)
        return charge

    def charge_token(self, account: str, token: str, amount: int, currency: str, transaction_id: str,
                     open_checkout_hash: str | None = None) -> Charge:
        """An AP2 token pays one checkout, for its approved amount, to its approved payee, once."""
        with self._lock:
            return self._charge_token(account, token, amount, currency, transaction_id, open_checkout_hash)

    def _charge_token(self, account: str, token: str, amount: int, currency: str, transaction_id: str,
                      open_checkout_hash: str | None = None) -> Charge:
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
        scope.used = True
        charge = Charge(account, amount, currency, "ap2", transaction_id)
        self.charges.append(charge)
        return charge


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

    def redeem(self, token: str) -> _Scope | None:
        scope = self._tokens.get(token)
        if scope is None or scope.used:
            return None
        return scope
