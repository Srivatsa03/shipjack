"""The assistant's own tables, in Postgres, in their own schema (``assistant``) beside the store's.

LangGraph keeps each conversation's state in its checkpoint tables (created by its saver). These
tables hold what the web app lists and the operator console reviews: users, threads, approvals, the
orders each thread placed (kept current by the store's signed webhooks), and the event log behind
the live timeline and the metrics.
"""

from __future__ import annotations

from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
                                  address JSONB, token TEXT UNIQUE NOT NULL, role TEXT NOT NULL DEFAULT 'shopper',
                                  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                                    title TEXT NOT NULL DEFAULT 'New chat', created TIMESTAMPTZ NOT NULL DEFAULT now(),
                                    updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                                      kind TEXT NOT NULL, actor TEXT NOT NULL, payload JSONB NOT NULL,
                                      status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, note TEXT,
                                      created TIMESTAMPTZ NOT NULL DEFAULT now(), decided TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, user_id TEXT NOT NULL,
                                   status TEXT NOT NULL, data JSONB NOT NULL, signed_at BIGINT NOT NULL DEFAULT 0,
                                   created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS events (seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, thread_id TEXT NOT NULL,
                                   run_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(), event TEXT NOT NULL,
                                   node TEXT, data JSONB NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_thread ON events (thread_id, seq);
CREATE TABLE IF NOT EXISTS webhook_seen (signature TEXT PRIMARY KEY, request_id TEXT, t BIGINT NOT NULL);
ALTER TABLE users ADD COLUMN IF NOT EXISTS sandbox TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ip TEXT;
"""

GUEST_ADDRESS = {"name": "Demo Shopper", "line_one": "1 Demo Way", "city": "Springfield", "state": "IL",
                 "postal_code": "62701", "country": "US"}

DEMO_USERS = [
    {"id": "u_sam", "name": "Sam Rivera", "email": "sam.rivera@example.com", "token": "demo-shopper",
     "role": "shopper", "address": {"name": "Sam Rivera", "line_one": "1200 W Harrison St", "city": "Chicago",
                                    "state": "IL", "postal_code": "60607", "country": "US"}},
    {"id": "u_ops", "name": "Northwind Ops", "email": "ops@northwind.example", "token": "demo-operator",
     "role": "operator", "address": None},
]


class AssistantDB:
    """Async access to the assistant's tables, through a psycopg connection pool."""

    def __init__(self, pool):
        self.pool = pool

    async def setup(self, seed: bool = True) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA, prepare=False)
            if seed:
                for u in DEMO_USERS:
                    await conn.execute("INSERT INTO users (id, name, email, address, token, role) VALUES "
                                       "(%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                                       (u["id"], u["name"], u["email"], _j(u["address"]), u["token"], u["role"]))

    async def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            cols = [c.name for c in cur.description] if cur.description else []
            return [dict(row) if isinstance(row, dict) else dict(zip(cols, row, strict=True))
                    for row in await cur.fetchall()]

    async def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = await self._all(sql, args)
        return rows[0] if rows else None

    async def _run(self, sql: str, args: tuple = ()) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, args)

    # ---- users and threads -------------------------------------------------------------------
    async def user_by_token(self, token: str) -> dict | None:
        return await self._one("SELECT id, name, email, address, role, sandbox FROM users WHERE token = %s", (token,))

    # ---- public demo: one private sandbox per visitor --------------------------------------------
    async def create_guest(self, ip: str) -> dict:
        """A shopper and an operator who sees only that shopper's chats and orders."""
        import secrets
        gid = f"g_{secrets.token_hex(6)}"
        shopper, operator = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        async with self.pool.connection() as conn:
            await conn.execute("INSERT INTO users (id, name, email, address, token, role, sandbox, ip) VALUES "
                               "(%s, %s, %s, %s, %s, 'shopper', %s, %s)",
                               (gid, "Demo Shopper", f"{gid}@demo.shipjack.example", _j(GUEST_ADDRESS), shopper, gid, ip))
            await conn.execute("INSERT INTO users (id, name, email, token, role, sandbox, ip) VALUES "
                               "(%s, %s, %s, %s, 'operator', %s, %s)",
                               (f"{gid}_ops", "Store operator (your sandbox)", f"{gid}-ops@demo.shipjack.example",
                                operator, gid, ip))
        return {"shopper_token": shopper, "operator_token": operator, "user_id": gid}

    async def guests_from(self, ip: str, hours: float = 1.0) -> int:
        row = await self._one("SELECT count(*) AS n FROM users WHERE ip = %s AND role = 'shopper' AND "
                              "created > now() - make_interval(secs => %s)", (ip, hours * 3600))
        return row["n"] if row else 0

    async def model_calls_today(self, user_id: str | None = None) -> int:
        """Model calls since midnight UTC, for one user's threads or for everyone."""
        sql = ("SELECT count(*) AS n FROM events e" + (" JOIN threads t ON t.id = e.thread_id AND t.user_id = %s"
                                                         if user_id else "") +
               " WHERE e.event = 'model_call' AND e.ts >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'")
        row = await self._one(sql, (user_id,) if user_id else ())
        return row["n"] if row else 0

    async def set_address(self, user_id: str, address: dict) -> None:
        await self._run("UPDATE users SET address = %s WHERE id = %s", (_j(address), user_id))

    async def create_thread(self, thread_id: str, user_id: str) -> dict:
        await self._run("INSERT INTO threads (id, user_id) VALUES (%s, %s)", (thread_id, user_id))
        return await self.thread(thread_id)

    async def thread(self, thread_id: str) -> dict | None:
        return await self._one("SELECT id, user_id, title, created, updated FROM threads WHERE id = %s", (thread_id,))

    async def threads(self, user_id: str) -> list[dict]:
        return await self._all("SELECT id, title, created, updated FROM threads WHERE user_id = %s "
                               "ORDER BY updated DESC LIMIT 50", (user_id,))

    async def touch(self, thread_id: str, title: str | None = None) -> None:
        if title:
            await self._run("UPDATE threads SET updated = now(), title = CASE WHEN title = 'New chat' THEN %s "
                            "ELSE title END WHERE id = %s", (title[:80], thread_id))
        else:
            await self._run("UPDATE threads SET updated = now() WHERE id = %s", (thread_id,))

    # ---- approvals ---------------------------------------------------------------------------
    async def add_approval(self, approval_id: str, thread_id: str, payload: dict) -> None:
        await self._run("INSERT INTO approvals (id, thread_id, kind, actor, payload) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (approval_id, thread_id, payload.get("kind", "?"), payload.get("actor", "?"), _j(payload)))

    async def approvals(self, status: str | None = None, actor: str | None = None,
                        thread_id: str | None = None, sandbox: str | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("status", status), ("actor", actor), ("thread_id", thread_id)):
            if val:
                where.append(f"{col} = %s")
                args.append(val)
        if sandbox:
            where.append("thread_id IN (SELECT id FROM threads WHERE user_id = %s)")
            args.append(sandbox)
        sql = ("SELECT id, thread_id, kind, actor, payload, status, decided_by, note, created, decided FROM approvals"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT 200")
        return await self._all(sql, tuple(args))

    async def decide(self, approval_id: str, approved: bool, by: str, note: str = "",
                     max_age_s: float | None = None) -> dict | None:
        """Record a decision once. Returns the approval, or None if it was not pending (already decided,
        or older than ``max_age_s``, in which case it stays pending for the sweeper to expire)."""
        age = "" if max_age_s is None else " AND created > now() - make_interval(secs => %s)"
        args = ("approved" if approved else "declined", by, note[:300], approval_id) + (() if max_age_s is None else (max_age_s,))
        return await self._one("UPDATE approvals SET status = %s, decided_by = %s, note = %s, decided = now() "
                               "WHERE id = %s AND status = 'pending'" + age +
                               " RETURNING id, thread_id, kind, actor, payload", args)

    async def expire(self, kind: str, max_age_s: float) -> list[dict]:
        """Mark pending approvals of ``kind`` older than ``max_age_s`` as expired; returns them."""
        return await self._all("UPDATE approvals SET status = 'expired', decided_by = 'expiry', decided = now() "
                               "WHERE kind = %s AND status = 'pending' AND created < now() - make_interval(secs => %s) "
                               "RETURNING id, thread_id", (kind, max_age_s))

    # ---- orders ------------------------------------------------------------------------------
    async def add_order(self, order_id: str, thread_id: str, user_id: str, data: dict) -> None:
        await self._run("INSERT INTO orders (id, thread_id, user_id, status, data) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING", (order_id, thread_id, user_id, "created", _j(data)))

    async def order_ids(self, user_id: str) -> list[str]:
        return [r["id"] for r in await self._all("SELECT id FROM orders WHERE user_id = %s ORDER BY created", (user_id,))]

    async def approval(self, approval_id: str) -> dict | None:
        return await self._one("SELECT a.id, a.thread_id, a.kind, a.actor, a.status, t.user_id FROM approvals a "
                               "JOIN threads t ON t.id = a.thread_id WHERE a.id = %s", (approval_id,))

    async def order_owner(self, order_id: str) -> str | None:
        row = await self._one("SELECT user_id FROM orders WHERE id = %s", (order_id,))
        return row["user_id"] if row else None

    async def orders(self, user_id: str | None = None, thread_id: str | None = None) -> list[dict]:
        where, args = [], []
        if user_id:
            where.append("user_id = %s")
            args.append(user_id)
        if thread_id:
            where.append("thread_id = %s")
            args.append(thread_id)
        return await self._all("SELECT id, thread_id, user_id, status, data, created, updated FROM orders"
                               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT 200",
                               tuple(args))

    async def apply_order_event(self, order: dict, signed_at: int) -> bool:
        """Apply a webhook's order snapshot unless a newer one was already applied. True if applied."""
        async with self.pool.connection() as conn:
            cur = await conn.execute("UPDATE orders SET status = %s, data = %s, signed_at = %s, updated = now() "
                                     "WHERE id = %s AND signed_at <= %s",
                                     (order.get("status", "?"), _j(order), signed_at, order["id"], signed_at))
            return cur.rowcount == 1

    async def seen_webhook(self, signature: str, request_id: str | None, t: int) -> bool:
        """Remember a delivery; True if this signature or Request-Id was seen before (a replay or a retry)."""
        async with self.pool.connection() as conn:
            if request_id:
                cur = await conn.execute("SELECT 1 FROM webhook_seen WHERE request_id = %s", (request_id,))
                if await cur.fetchone():
                    return True
            cur = await conn.execute("INSERT INTO webhook_seen (signature, request_id, t) VALUES (%s, %s, %s) "
                                     "ON CONFLICT (signature) DO NOTHING", (signature, request_id, t))
            return cur.rowcount == 0

    # ---- events and metrics ------------------------------------------------------------------
    async def add_event(self, thread_id: str, run_id: str, event: str, node: str | None, data: dict) -> None:
        await self._run("INSERT INTO events (thread_id, run_id, event, node, data) VALUES (%s, %s, %s, %s, %s)",
                        (thread_id, run_id, event, node, _j(data)))

    async def events(self, thread_id: str, after: int = 0) -> list[dict]:
        return await self._all("SELECT seq, run_id, ts, event, node, data FROM events WHERE thread_id = %s AND seq > %s "
                               "ORDER BY seq LIMIT 1000", (thread_id, after))

    async def metrics(self) -> dict:
        calls = await self._one("""
            SELECT count(*) AS model_calls,
                   coalesce(sum((data->>'tokens_in')::int), 0) AS tokens_in,
                   coalesce(sum((data->>'tokens_out')::int), 0) AS tokens_out,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY (data->>'latency_ms')::int) AS p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY (data->>'latency_ms')::int) AS p95_ms
            FROM events WHERE event = 'model_call'""")
        counts = await self._one("""
            SELECT (SELECT count(*) FROM threads) AS threads, (SELECT count(*) FROM orders) AS orders,
                   (SELECT count(*) FROM approvals WHERE status = 'pending') AS pending_approvals,
                   (SELECT count(*) FROM approvals WHERE kind = 'purchase' AND status = 'approved') AS purchases_approved,
                   (SELECT count(*) FROM approvals WHERE kind = 'purchase' AND status = 'declined') AS purchases_declined,
                   (SELECT count(*) FROM events WHERE event IN ('checkout_refused', 'proposal_refused')) AS refused,
                   (SELECT count(*) FROM events WHERE event IN ('model_error', 'payment_error', 'refund_error')) AS errors""")
        return {**(counts or {}), **{k: (float(v) if v is not None else None) for k, v in (calls or {}).items()}}


def _j(value: Any):
    from psycopg.types.json import Jsonb
    return None if value is None else Jsonb(value)
