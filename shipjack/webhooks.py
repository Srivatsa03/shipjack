"""ACP order webhooks: the merchant tells the agent platform what happened to an order after checkout.

The agent platform runs a receiver, and the merchant POSTs one event to it per order change. This
follows the ACP 2026-04-17 webhook spec:

* the body is ``{"type": "order_create" | "order_update", "data": <the full Order>}``, a full
  snapshot of the order, never a delta;
* the ``Merchant-Signature`` header is ``t=<unix_seconds>,v1=<64 hex>``, the HMAC-SHA256 of
  ``t + "." + raw_body`` under a secret the two sides share;
* the receiver answers 401 ``invalid_signature`` to a missing, malformed, stale (outside a 300 s
  window) or wrong signature, and ``200 {"received": true}`` to a good one.

What a real integration needs on top of the spec, and what this module adds:

* **An outbox with at-least-once delivery.** A network error, 408, 425, 429 or 5xx is retried with
  exponential backoff. Any other answer means the event itself is wrong, so the event goes to the
  dead-letter list at once instead of being retried forever.
* **Per-order ordering.** An order's next event waits until the earlier one is delivered or dead,
  so a retried "created" can never land after "shipped".
* **One Request-Id per event.** Every retry of an event carries the same ``Request-Id``, so the
  receiver can drop duplicates.
* **A durable outbox, one sender.** With a SQLite store (``SqliteQueue``) an event is written in the
  same transaction as the order change, so the two commit together or not at all and survive a
  restart. Server processes share one queue, and a lease lets one of them send at a time, so
  per-order ordering holds across processes too.
* **A receiver that refuses replays.** ``Request-Id`` is outside the signature, so an attacker who
  captured a request can change it. The reference receiver therefore also remembers the signatures
  it accepted, and it never lets an older snapshot of an order (an earlier ``t``) replace a newer one.

The secret comes from the environment (``SHIPJACK_WEBHOOK_SECRET``), never from a command line.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SIGNATURE_HEADER = "Merchant-Signature"
RECEIVER_PATH = "/agentic_checkout/webhooks/order_events"
TOLERANCE = 300  # seconds; the spec's recommended timestamp window
SECRET_ENV = "SHIPJACK_WEBHOOK_SECRET"
MIN_SECRET_BYTES = 32
RETRYABLE = frozenset({408, 425, 429})  # plus every 5xx and every network error

_HEADER = re.compile(r"^t=(\d{1,12}),v1=([a-fA-F0-9]{64})$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
log = logging.getLogger("shipjack.webhooks")


class SignatureError(Exception):
    """The request is not signed by the shared secret, or not recently. The message is the spec's."""


def sign(secret: bytes, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(secret: bytes, body: bytes, header: str | None, *, now: float | None = None,
           tolerance: int = TOLERANCE) -> tuple[int, str]:
    """Check a ``Merchant-Signature`` header over the raw body. Returns (timestamp, signature hex)."""
    if not header:
        raise SignatureError("Missing Merchant-Signature header.")
    match = _HEADER.match(header.strip())
    if not match:
        raise SignatureError("Merchant-Signature must be t=<timestamp>,v1=<64_hex>.")
    timestamp, given = int(match.group(1)), match.group(2).lower()
    if abs((time.time() if now is None else now) - timestamp) > tolerance:
        raise SignatureError("Timestamp outside allowed window.")
    expected = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise SignatureError("Webhook signature verification failed.")
    return timestamp, given


def secret_from_env(var: str = SECRET_ENV) -> bytes:
    """The shared secret, from the environment. Refuses a missing or short one."""
    value = os.environ.get(var, "")
    if not value:
        raise ValueError(f"set {var} to the webhook secret you share with the agent platform")
    secret = value.encode()
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(f"{var} is shorter than {MIN_SECRET_BYTES} bytes; use e.g. `openssl rand -hex 32`")
    return secret


def _check_url(url: str) -> str:
    """Signed order data leaves only over HTTPS. Plain HTTP is allowed only to this machine, for testing."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS:
        return url
    raise ValueError("webhook URL must be https:// (plain http:// only to localhost)")


def _http_send(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    import httpx
    return httpx.post(url, content=body, headers=headers, timeout=timeout, follow_redirects=False).status_code


@dataclass
class Delivery:
    id: str            # evt_..., sent as Request-Id on every attempt
    order_id: str
    type: str
    body: bytes        # the snapshot, fixed when the change happened; only the signature is renewed
    created: float
    attempts: int = 0
    next_at: float = 0.0
    state: str = "pending"  # pending | delivered | dead
    last_status: int | None = None
    last_error: str = ""


class MemoryQueue:
    """Pending events in this process: one FIFO per order."""

    def __init__(self):
        self._queues: dict[str, deque[Delivery]] = {}
        self._lock = threading.Lock()

    def push(self, d: Delivery) -> None:
        with self._lock:
            self._queues.setdefault(d.order_id, deque()).append(d)

    def due(self, now: float, skip: set[str]) -> list[Delivery]:
        with self._lock:
            return [q[0] for oid, q in self._queues.items() if oid not in skip and q[0].next_at <= now]

    def finish(self, d: Delivery, outcome: str) -> None:
        if outcome == "retry":
            return  # it stays at the head, with its new next_at
        with self._lock:
            queue = self._queues[d.order_id]
            assert queue[0] is d
            queue.popleft()
            if not queue:
                del self._queues[d.order_id]

    def pending(self) -> list[Delivery]:
        with self._lock:
            return [d for q in self._queues.values() for d in q]

    def acquire(self, holder: str, now: float, ttl: float) -> bool:
        return True  # one process, one sender

    def release(self, holder: str) -> None:
        pass


class SqliteQueue:
    """Pending events in the store's database (SQLite or Postgres), written in the same transaction as
    the order change.

    ``name`` is the destination (one queue per receiver URL). Every server process can queue; a lease
    (``leases`` table) lets one of them send at a time.
    """

    def __init__(self, db, name: str):
        self.db, self.name, self.lease = db, name, f"webhooks:{name}"

    def push(self, d: Delivery) -> None:
        self.db.write("INSERT INTO outbox (id, queue, order_id, type, body, created, next_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (d.id, self.name, d.order_id, d.type, d.body, d.created, d.next_at))

    _COLUMNS = "id, order_id, type, body, created, attempts, next_at, state, last_status, last_error"

    def _rows(self, where: str, args: tuple) -> list[Delivery]:
        rows = self.db.read(f"SELECT {self._COLUMNS} FROM outbox o WHERE queue = ? AND {where} ORDER BY seq",
                            (self.name, *args))
        return [Delivery(r[0], r[1], r[2], bytes(r[3]), r[4], r[5], r[6], r[7], r[8], r[9]) for r in rows]

    def due(self, now: float, skip: set[str]) -> list[Delivery]:
        heads = self._rows("state = 'pending' AND next_at <= ? AND seq = (SELECT MIN(seq) FROM outbox p "
                           "WHERE p.queue = o.queue AND p.order_id = o.order_id AND p.state = 'pending')", (now,))
        return [d for d in heads if d.order_id not in skip]

    def finish(self, d: Delivery, outcome: str) -> None:
        self.db.write("UPDATE outbox SET state = ?, attempts = ?, next_at = ?, last_status = ?, last_error = ? "
                      "WHERE id = ?", (d.state, d.attempts, d.next_at, d.last_status, d.last_error, d.id))

    def pending(self) -> list[Delivery]:
        return self._rows("state = 'pending'", ())

    def settled(self, state: str) -> list[Delivery]:
        return self._rows("state = ?", (state,))

    def acquire(self, holder: str, now: float, ttl: float) -> bool:
        """Take or renew the sender lease. It is free when nobody holds it or the holder let it expire."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT holder, expires FROM leases WHERE name = ?", (self.lease,)).fetchone()
            if row is not None and row[0] != holder and row[1] >= now:
                return False
            conn.execute("INSERT INTO leases (name, holder, expires) VALUES (?, ?, ?) ON CONFLICT (name) "
                         "DO UPDATE SET holder = excluded.holder, expires = excluded.expires",
                         (self.lease, holder, now + ttl))
            return True

    def release(self, holder: str) -> None:
        """Give the lease up now (a clean shutdown), so another process need not wait for it to expire."""
        self.db.write("DELETE FROM leases WHERE name = ? AND holder = ?", (self.lease, holder))


@dataclass
class Outbox:
    """Signed, ordered, retried delivery of order events to one agent platform's receiver."""

    url: str
    secret: bytes
    send: Callable[[str, bytes, dict[str, str], float], int] = _http_send
    max_attempts: int = 8
    base_delay: float = 1.0
    max_delay: float = 300.0
    jitter: bool = True
    timeout: float = 10.0
    clock: Callable[[], float] = time.time
    queue: MemoryQueue | SqliteQueue | None = None
    lease_ttl: float = 30.0
    delivered: list[Delivery] = field(default_factory=list)  # what this process delivered
    dead: list[Delivery] = field(default_factory=list)       # what this process gave up on
    attempts_log: list[dict] = field(default_factory=list)
    _holder: str = field(default_factory=lambda: f"{os.getpid()}-{secrets.token_hex(6)}", repr=False)
    _run_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def __post_init__(self):
        _check_url(self.url)
        if len(self.secret) < MIN_SECRET_BYTES:
            raise ValueError(f"webhook secret must be at least {MIN_SECRET_BYTES} bytes")

    # ---- the merchant side -------------------------------------------------------------------
    def attach(self, merchant) -> Outbox:
        """Listen to a merchant. Unless a queue was given, the events are kept where the merchant keeps
        its state (in SQLite, in the same transaction as the order change)."""
        if self.queue is None:
            self.queue = merchant.repo.outbox_queue(self.url)
        merchant.listeners.append(self)
        return self

    def detach(self, merchant) -> None:
        merchant.listeners.remove(self)

    def __call__(self, event_type: str, order: dict) -> None:
        """A Merchant listener: queue the event, inside the merchant's transaction."""
        self.enqueue(event_type, order)

    def enqueue(self, event_type: str, order: dict) -> Delivery:
        body = json.dumps({"type": event_type, "data": order}, separators=(",", ":")).encode()
        now = self.clock()
        delivery = Delivery("evt_" + secrets.token_hex(12), order["id"], event_type, body, now, next_at=now)
        if self.queue is None:
            self.queue = MemoryQueue()
        self.queue.push(delivery)
        return delivery

    def pending(self) -> int:
        return len(self.queue.pending()) if self.queue is not None else 0

    # ---- delivery ------------------------------------------------------------------------------
    def run_once(self) -> int:
        """Send each order's oldest event that is due, then the next, until nothing is due. Returns how
        many events were delivered. No lock or transaction is held during a network call."""
        delivered, held = 0, set()  # held: orders whose head is waiting to retry
        if self.queue is None:
            return 0
        with self._run_lock:
            while self.queue.acquire(self._holder, self.clock(), self.lease_ttl):
                heads = self.queue.due(self.clock(), held)
                if not heads:
                    break
                for head in heads:
                    if not self.queue.acquire(self._holder, self.clock(), self.lease_ttl):
                        return delivered  # another process took over the sending
                    outcome = self._attempt(head)
                    self.queue.finish(head, outcome)
                    if outcome == "delivered":
                        delivered += 1
                    elif outcome == "retry":
                        held.add(head.order_id)  # later events for this order wait behind this one
        return delivered

    def _attempt(self, d: Delivery) -> str:
        timestamp = int(self.clock())
        headers = {"Content-Type": "application/json", SIGNATURE_HEADER: sign(self.secret, d.body, timestamp),
                   "Request-Id": d.id, "Timestamp": datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "User-Agent": "shipjack-webhooks"}
        d.attempts += 1
        try:
            status, error = self.send(self.url, d.body, headers, self.timeout), ""
        except Exception as exc:  # any transport failure is worth another try
            status, error = None, type(exc).__name__
        d.last_status, d.last_error = status, error
        if status is not None and 200 <= status < 300:
            outcome = "delivered"
            d.state = "delivered"
            self.delivered.append(d)
        elif (status is None or status >= 500 or status in RETRYABLE) and d.attempts < self.max_attempts:
            outcome = "retry"
            delay = min(self.max_delay, self.base_delay * 2 ** (d.attempts - 1))
            d.next_at = self.clock() + (delay * random.uniform(0.5, 1.0) if self.jitter else delay)
        else:
            outcome = "dead"
            d.state = "dead"
            self.dead.append(d)
            log.warning("webhook %s for %s is dead after %d attempt(s): status=%s %s", d.id, d.order_id,
                        d.attempts, status, error)
        self.attempts_log.append({"event": d.id, "order": d.order_id, "type": d.type, "attempt": d.attempts,
                                  "status": status, "error": error, "outcome": outcome})
        return outcome

    def drain(self, timeout: float = 10.0, interval: float = 0.05) -> bool:
        """Deliver until nothing is pending or the time runs out (wall clock). True when empty."""
        end = time.monotonic() + timeout
        while self.pending() and time.monotonic() < end:
            self.run_once()
            if self.pending():
                time.sleep(interval)
        return not self.pending()

    def start(self, interval: float = 0.5) -> Outbox:
        """Run delivery in a background thread until ``stop``."""
        def loop():
            while not self._stop.wait(interval):
                try:
                    self.run_once()
                except Exception:  # never let one bad event stop delivery for everyone
                    log.exception("webhook delivery loop error")
        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="shipjack-webhooks", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.queue is not None:
            self.queue.release(self._holder)


# ---- the agent platform side: a reference receiver -------------------------------------------------


@dataclass
class ReceiverState:
    orders: dict[str, tuple[int, dict]] = field(default_factory=dict)  # order id -> (signed t, latest Order)
    events: list[dict] = field(default_factory=list)                    # every applied event, in order
    duplicates: int = 0
    stale: int = 0
    _signatures: dict[str, int] = field(default_factory=dict)  # accepted signature -> its t
    _request_ids: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def create_receiver(secret: bytes, *, tolerance: int = TOLERANCE, path: str = RECEIVER_PATH,
                    on_event: Callable[[dict], None] | None = None):
    """A FastAPI app that receives ACP order webhooks the way an agent platform should.

    ``on_event`` is called with each event the receiver applies (not with duplicates or stale ones).
    """
    app = FastAPI(title="ACP order webhook receiver (reference)")
    state = ReceiverState()
    app.state.receiver = state

    def error(status: int, code: str, message: str, param: str | None = None) -> JSONResponse:
        body = {"type": "invalid_request", "code": code, "message": message, **({"param": param} if param else {})}
        return JSONResponse(body, status_code=status)

    @app.post(path)
    async def receive(request: Request):
        raw = await request.body()
        try:
            timestamp, signature = verify(secret, raw, request.headers.get(SIGNATURE_HEADER), tolerance=tolerance)
        except SignatureError as exc:
            return error(401, "invalid_signature", str(exc))
        if not request.headers.get("content-type", "").startswith("application/json"):
            return error(400, "invalid", "Content-Type must be application/json")
        try:
            event = json.loads(raw)
        except ValueError:
            return error(400, "invalid", "body is not valid JSON")
        problem = _event_problem(event)
        if problem:
            return error(400, "invalid", problem[1], problem[0])
        request_id = request.headers.get("request-id") or None
        reply = {"received": True, **({"request_id": request_id} if request_id else {})}
        order, now = event["data"], int(time.time())
        with state._lock:
            for seen in (state._signatures, state._request_ids):  # forget what the window already refuses
                for key in [k for k, t in seen.items() if t < now - 2 * tolerance]:
                    del seen[key]
            if signature in state._signatures or (request_id and request_id in state._request_ids):
                state.duplicates += 1
                return reply
            state._signatures[signature] = timestamp
            if request_id:
                state._request_ids[request_id] = timestamp
            latest = state.orders.get(order["id"])
            if latest and timestamp < latest[0]:
                state.stale += 1  # an older snapshot never replaces a newer one
                return reply
            state.orders[order["id"]] = (timestamp, order)
            state.events.append(event)
        if on_event:
            on_event(event)
        return reply

    return app


def _event_problem(event) -> tuple[str, str] | None:
    """The fields a receiver needs before it can apply an event. Unknown event types and statuses are
    accepted, as the spec requires ("Implementations MUST accept unrecognized values gracefully")."""
    if not isinstance(event, dict):
        return "$", "body must be a JSON object"
    if not isinstance(event.get("type"), str):
        return "$.type", "type must be text"
    data = event.get("data")
    if not isinstance(data, dict):
        return "$.data", "data must be an Order object"
    if data.get("type") != "order":
        return "$.data.type", "data.type must be 'order'"
    for key in ("id", "checkout_session_id", "permalink_url", "status"):
        if not isinstance(data.get(key), str) or not data[key]:
            return f"$.data.{key}", f"data.{key} is required"
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the reference receiver and print each order event it accepts (for trying the webhooks out)."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(prog="shipjack-receiver",
                                     description="Receive and verify ACP order webhooks (secret: SHIPJACK_WEBHOOK_SECRET).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print("shipjack-receiver needs uvicorn: pip install 'shipjack[serve]'", file=sys.stderr)
        return 2
    try:
        secret = secret_from_env()
    except ValueError as exc:
        print(f"shipjack-receiver: {exc}", file=sys.stderr)
        return 2

    def show(event: dict) -> None:
        order = event["data"]
        print(f"{event['type']:<13} {order['id']}  status={order['status']}", flush=True)

    print(f"receiver on http://{args.host}:{args.port}{RECEIVER_PATH}", flush=True)
    uvicorn.run(create_receiver(secret, on_event=show), host=args.host, port=args.port, log_level="warning")
    return 0
