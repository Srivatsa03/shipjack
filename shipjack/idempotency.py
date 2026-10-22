"""Idempotency keys, shared by the REST API and the MCP binding (ACP 2026-04-17, section 6).

A key's record lives in the store's database, so a retry finds it after a restart or on another server
process. A request claims its key in its first transaction and stores its answer in its last. A retry
with the key then gets that answer back (``Replay``); a retry while the first request is still running
gets ``InFlight``; the same key with a different request gets ``Conflict``.

A payment also keeps its key's record in the session's payment mark (``Key.ref``). If the process dies
between charging and recording, whoever finishes the payment (a retry with another key, or
``Merchant.reconcile_payments``) stores the answer under this key as well, so a retry with the original
key gets the receipt instead of an error.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

IN_FLIGHT_TTL_S = 300.0  # a claimed key whose request died is free again after this
RETRY_AFTER_S = 1  # what an in-flight answer tells the client to wait, in seconds


def canonical(value: Any) -> Any:
    """ACP request equivalence: key order and trailing zeros do not matter; null and absent differ."""
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def fingerprint(body: Any) -> str:
    """What a key's request is compared by: a retry must send the same body."""
    return hashlib.sha256(json.dumps(canonical(body), separators=(",", ":")).encode()).hexdigest()


class Conflict(Exception):
    """The key was already used with a different request."""


class InFlight(Exception):
    """A request with this key is still running."""


@dataclass
class Replay:
    """The stored answer for a key."""
    code: int
    payload: Any


class Key:
    """One request's idempotency key: ``slot`` names it (caller, operation, key), ``fingerprint`` is
    the request it was used with, and ``status`` is what a success answers with."""

    def __init__(self, repo, merchant_id: str, slot: tuple, fingerprint: str, status: int = 200):
        self.repo, self.slot, self.fingerprint, self.status = repo, tuple(slot), fingerprint, status
        # the key's own lock: two requests with one key wait for each other, whatever else they lock
        self.keys = (f"idem:{merchant_id}:" + hashlib.sha256(json.dumps(list(self.slot)).encode()).hexdigest(),)

    def start(self, claim: bool = True) -> Replay | None:
        """Claim the key (returns None: go ahead), or answer from it. Call it inside a transaction that
        holds ``self.keys``. ``claim=False`` when the whole request is that one transaction: its lock
        is the claim, and a fault leaves nothing behind."""
        seen = self.repo.idempotent(self.slot)
        if seen is not None:
            fingerprint, code, payload = seen
            if fingerprint != self.fingerprint:
                raise Conflict()
            if code != 0:
                return Replay(code, payload)
            if time.time() - payload.get("since", 0) < IN_FLIGHT_TTL_S:
                raise InFlight()
        if claim:
            self.repo.remember(self.slot, self.fingerprint, 0, {"since": time.time()})  # 0: in progress
        return None

    def finish(self, payload: Any, code: int | None = None) -> None:
        self.repo.remember(self.slot, self.fingerprint, self.status if code is None else code, payload)

    def forget(self) -> None:
        """Free the key: the request was refused before it did anything, or it failed, so a retry runs."""
        self.repo.forget(self.slot)

    def ref(self) -> dict:
        """What a payment mark keeps, so that whoever finishes the payment can answer this key."""
        return {"slot": list(self.slot), "fingerprint": self.fingerprint, "status": self.status}


def answer(repo, ref: dict | None, payload: Any | None) -> None:
    """Store the answer for a payment mark's key (``payload`` None: free the key instead)."""
    if not ref:
        return
    if payload is None:
        repo.forget(tuple(ref["slot"]))
    else:
        repo.remember(tuple(ref["slot"]), ref["fingerprint"], ref["status"], payload)
