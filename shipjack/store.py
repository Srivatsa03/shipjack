"""Where a merchant keeps its state: in this process (the default), or in a database (SQLite or
Postgres), so the state survives a restart and several server processes can share one store.

The merchant talks to a *repository*, and every change runs inside ``repo.transaction()``:

* ``MemoryRepo`` is one re-entrant lock around plain dicts and lists (the original behaviour).
* ``SqliteRepo`` turns the same transaction into one database transaction that admits one writer at a
  time across every process: ``BEGIN IMMEDIATE`` on a SQLite file in WAL mode, or a transaction holding
  a Postgres advisory lock. So "is this session still payable?" and "record the charge" are one step
  even with several server processes racing for the same session. Objects read inside a transaction
  are written back when it commits; if anything raises, nothing is written.

``open_database`` picks the backend: a ``postgresql://`` URL, or a SQLite file path.

What the database holds: checkout sessions, orders and their payment records, receipts, the audit
trail, idempotency responses, the webhook outbox, id counters, the merchant's checkout-signing key (so
every process and every restart signs with the same key), and the simulated processor's ledger. Card
numbers are never stored. In production the signing key belongs in a KMS, not in this file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (merchant TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                     PRIMARY KEY (merchant, id));
CREATE TABLE IF NOT EXISTS orders (seq INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL,
                                   id TEXT NOT NULL, data TEXT NOT NULL, record TEXT NOT NULL,
                                   UNIQUE (merchant, id));
CREATE TABLE IF NOT EXISTS receipts (seq INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (merchant TEXT NOT NULL, caller TEXT NOT NULL, path TEXT NOT NULL,
                                        key TEXT NOT NULL, fingerprint TEXT NOT NULL, code INTEGER NOT NULL,
                                        payload TEXT NOT NULL, PRIMARY KEY (merchant, caller, path, key));
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, n INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, jwk TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_charges (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                                           data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_refunds (seq INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                                   queue TEXT NOT NULL, order_id TEXT NOT NULL, type TEXT NOT NULL, body BLOB NOT NULL,
                                   created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                                   next_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                                   last_status INTEGER, last_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (queue, state, order_id, seq);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS catalog (merchant TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                    PRIMARY KEY (merchant, id));
"""

POSTGRES_SCHEMA = (SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
                   .replace("BLOB", "BYTEA").replace("REAL", "DOUBLE PRECISION"))


# ---- in this process ---------------------------------------------------------------------------------


class MemoryRepo:
    """State in this process, one re-entrant lock around it."""

    def __init__(self):
        self._lock = threading.RLock()
        self.sessions: dict = {}
        self.orders: list[dict] = []
        self.receipts: list[dict] = []
        self.audit: list[dict] = []
        self._orders: dict[str, dict] = {}
        self._records: dict[str, dict] = {}
        self._idempotency: dict[tuple, tuple] = {}
        self._count = 0
        self._catalog: list[dict] | None = None

    def transaction(self, write: bool = True):
        return self._lock

    def signing_key(self, name: str, make: Callable):
        return make()

    def next_number(self) -> int:
        self._count += 1
        return self._count

    def session(self, session_id: str):
        return self.sessions.get(session_id)

    def add_session(self, session) -> None:
        self.sessions[session.id] = session

    def order(self, order_id: str) -> dict | None:
        return self._orders.get(order_id)

    def record(self, order_id: str) -> dict | None:
        return self._records.get(order_id)

    def add_order(self, order: dict, record: dict) -> None:
        self.orders.append(order)
        self._orders[order["id"]] = order
        self._records[order["id"]] = record

    def add_receipt(self, receipt: dict) -> None:
        self.receipts.append(receipt)

    def log(self, entry: dict) -> None:
        self.audit.append(entry)

    def idempotent(self, slot: tuple) -> tuple | None:
        return self._idempotency.get(slot)

    def outbox_queue(self, name: str):
        from .webhooks import MemoryQueue
        return MemoryQueue()

    def catalog(self) -> list[dict] | None:
        return self._catalog

    def save_catalog(self, products: list[dict]) -> None:
        self._catalog = list(products)

    def remember(self, slot: tuple, fingerprint: str, code: int, payload: Any) -> None:
        self._idempotency[slot] = (fingerprint, code, payload)


# ---- in SQLite --------------------------------------------------------------------------------------


class _Database:
    """One database. A connection per thread; ``transaction`` nests, and only the outermost commits.

    SQL is written once, with ``?`` placeholders and upserts that both SQLite and Postgres accept.
    """

    dialect = ""

    def __init__(self):
        self._local = threading.local()
        self._all: list = []  # every connection opened, so close() can close them
        self._guard = threading.Lock()

    def _connect(self):
        raise NotImplementedError

    def _begin(self, conn, write: bool) -> None:
        raise NotImplementedError

    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            with self._guard:
                self._all.append(conn)
            self._local.conn, self._local.depth, self._local.flush, self._local.after = conn, 0, [], []
            self._local.writing = False
        return conn

    def close(self) -> None:
        with self._guard:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        self._local = threading.local()

    @property
    def active(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def on_commit(self, flush: Callable[[], None]) -> None:
        """Run ``flush`` just before the outermost transaction commits (it writes loaded objects back)."""
        if flush not in self._local.flush:
            self._local.flush.append(flush)

    @contextmanager
    def transaction(self, write: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.conn()
        if self._local.depth:
            if write and not self._local.writing:  # upgrading a read to a write can fail under contention
                raise RuntimeError("a write inside a read-only transaction; open the outer one with write=True")
            self._local.depth += 1
            try:
                yield conn
            finally:
                self._local.depth -= 1
            return
        self._begin(conn, write)
        self._local.depth, self._local.writing = 1, write
        try:
            yield conn
            for flush in self._local.flush:
                flush()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            after, self._local.after = self._local.after, []
            self._local.depth, self._local.flush, self._local.writing = 0, [], False
            for done in after:
                done()

    def after(self, done: Callable[[], None]) -> None:
        """Run ``done`` when the outermost transaction ends, committed or not (clears identity maps)."""
        if done not in self._local.after:
            self._local.after.append(done)

    def read(self, sql: str, args: tuple = ()) -> list[tuple]:
        if self.active:
            return self.conn().execute(sql, args).fetchall()
        with self.transaction(write=False) as conn:
            return conn.execute(sql, args).fetchall()

    def write(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, args)




class Database(_Database):
    """A SQLite file in WAL mode. ``BEGIN IMMEDIATE`` takes the one writer lock for a write transaction."""

    dialect = "sqlite"

    def __init__(self, path: str):
        super().__init__()
        self.path = str(path)
        self.conn().executescript(SQLITE_SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def _begin(self, conn, write: bool) -> None:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")


class _PgConnection:
    """A psycopg connection that takes this module's ``?`` placeholders."""

    def __init__(self, conn):
        self.raw = conn

    def execute(self, sql: str, args: tuple = ()):
        return self.raw.execute(sql.replace("?", "%s"), args)

    def close(self) -> None:
        self.raw.close()


class PostgresDatabase(_Database):
    """Postgres. A write transaction holds one transaction-scoped advisory lock, so writes are serialised
    across every process exactly as SQLite serialises them; reads see one snapshot. ``schema`` keeps the
    tables in their own namespace (the tests give each test its own)."""

    dialect = "postgres"
    LOCK = 0x5A1B_7AC5  # the store's writer lock
    _open: weakref.WeakSet = weakref.WeakSet()

    @classmethod
    def close_all(cls) -> None:
        """Close every connection any PostgresDatabase in this process opened (tests, shutdown)."""
        for db in list(cls._open):
            db.close()

    def __init__(self, url: str, schema: str | None = None):
        super().__init__()
        self.url, self.schema = url, schema
        PostgresDatabase._open.add(self)
        conn = self.conn()
        if schema:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            conn.execute(f'SET search_path TO "{schema}", public')
        conn.execute("SELECT pg_advisory_lock(?)", (self.LOCK,))  # one process creates the tables at a time
        try:
            conn.raw.execute(POSTGRES_SCHEMA)
        finally:
            conn.execute("SELECT pg_advisory_unlock(?)", (self.LOCK,))

    def _connect(self):
        import psycopg
        conn = psycopg.connect(self.url, autocommit=True)
        if self.schema:
            conn.execute(f'SET search_path TO "{self.schema}", public')
        return _PgConnection(conn)

    def _begin(self, conn, write: bool) -> None:
        if write:
            conn.execute("BEGIN")
            conn.execute("SELECT pg_advisory_xact_lock(?)", (self.LOCK,))
        else:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")

    def read(self, sql: str, args: tuple = ()) -> list[tuple]:
        return [tuple(r) for r in super().read(sql, args)]


def open_database(target: str) -> _Database:
    """A ``postgresql://`` (or ``postgres://``) URL opens Postgres; anything else is a SQLite file path."""
    if target.startswith(("postgresql://", "postgres://")):
        return PostgresDatabase(target)
    return Database(target)


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _session_to_dict(s) -> dict:
    f = s.fulfillment
    return {"id": s.id, "items": [list(i) for i in s.items], "buyer": s.buyer, "status": s.status,
            "fulfillment": {"name": f.name, "email": f.email, "phone": f.phone, "address": f.address,
                            "extra": f.extra},
            "checkout_jwt": s.checkout_jwt, "nonce": s.nonce, "order_id": s.order["id"] if s.order else None,
            "messages": s.messages, "attempts": s.attempts}


class SqliteRepo:
    """One merchant's state in a shared SQLite database."""

    def __init__(self, db: _Database, merchant_id: str):
        self.db, self.merchant = db, merchant_id
        self._local = threading.local()

    # an identity map per transaction: the same row read twice is the same object, and every object
    # read inside a write transaction is written back when it commits
    def _map(self) -> dict | None:
        if not self.db.active or not self.db._local.writing:
            return None
        loaded = getattr(self._local, "loaded", None)
        if loaded is None:
            loaded = self._local.loaded = {}
            self.db.on_commit(self._flush)
            self.db.after(self._forget)
        return loaded

    def _forget(self) -> None:
        self._local.loaded = None

    def _flush(self) -> None:
        conn = self.db.conn()
        for (kind, key), obj in (self._local.loaded or {}).items():
            if kind == "session":
                conn.execute("INSERT INTO sessions (merchant, id, data) VALUES (?, ?, ?) ON CONFLICT (merchant, id) "
                             "DO UPDATE SET data = excluded.data", (self.merchant, key, _dump(_session_to_dict(obj))))
            elif kind == "order":
                record = self._local.loaded[("record", key)]
                conn.execute("UPDATE orders SET data = ?, record = ? WHERE merchant = ? AND id = ?",
                             (_dump(obj), _dump(_record_to_dict(record)), self.merchant, key))

    def transaction(self, write: bool = True):
        return self.db.transaction(write)

    def signing_key(self, name: str, make: Callable):
        from jwcrypto.jwk import JWK
        with self.db.transaction() as conn:
            row = conn.execute("SELECT jwk FROM keys WHERE name = ?", (name,)).fetchone()
            if row is None:
                key = make()
                conn.execute("INSERT INTO keys (name, jwk) VALUES (?, ?)", (name, key.export_private()))
                return key
        return JWK.from_json(row[0])

    def next_number(self) -> int:
        with self.db.transaction() as conn:
            return conn.execute("INSERT INTO counters (name, n) VALUES (?, 1) ON CONFLICT (name) "
                                "DO UPDATE SET n = counters.n + 1 RETURNING n", (f"session:{self.merchant}",)).fetchone()[0]

    def session(self, session_id: str):
        loaded = self._map()
        if loaded is not None and ("session", session_id) in loaded:
            return loaded[("session", session_id)]
        rows = self.db.read("SELECT data FROM sessions WHERE merchant = ? AND id = ?", (self.merchant, session_id))
        if not rows:
            return None
        session = self._session_from_dict(json.loads(rows[0][0]))
        if loaded is not None:
            loaded[("session", session_id)] = session
        return session

    def _session_from_dict(self, d: dict):
        from .catalog import Fulfillment
        from .merchant import Session
        return Session(d["id"], [tuple(i) for i in d["items"]], Fulfillment(**d["fulfillment"]), d["buyer"],
                       d["status"], d["checkout_jwt"], d["nonce"],
                       self.order(d["order_id"]) if d["order_id"] else None, d["messages"], d["attempts"])

    def add_session(self, session) -> None:
        self._map()[("session", session.id)] = session

    def order(self, order_id: str) -> dict | None:
        found = self._load_order(order_id)
        return found[0] if found else None

    def record(self, order_id: str) -> dict | None:
        found = self._load_order(order_id)
        return found[1] if found else None

    def _load_order(self, order_id: str) -> tuple[dict, dict] | None:
        loaded = self._map()
        if loaded is not None and ("order", order_id) in loaded:
            return loaded[("order", order_id)], loaded[("record", order_id)]
        rows = self.db.read("SELECT data, record FROM orders WHERE merchant = ? AND id = ?", (self.merchant, order_id))
        if not rows:
            return None
        order, record = json.loads(rows[0][0]), _record_from_dict(json.loads(rows[0][1]))
        if loaded is not None:
            loaded[("order", order_id)], loaded[("record", order_id)] = order, record
        return order, record

    def add_order(self, order: dict, record: dict) -> None:
        self.db.conn().execute("INSERT INTO orders (merchant, id, data, record) VALUES (?, ?, ?, ?)",
                               (self.merchant, order["id"], _dump(order), _dump(_record_to_dict(record))))
        loaded = self._map()
        loaded[("order", order["id"])], loaded[("record", order["id"])] = order, record

    def add_receipt(self, receipt: dict) -> None:
        self.db.write("INSERT INTO receipts (merchant, data) VALUES (?, ?)", (self.merchant, _dump(receipt)))

    def log(self, entry: dict) -> None:
        self.db.write("INSERT INTO audit (merchant, data) VALUES (?, ?)", (self.merchant, _dump(entry)))

    def idempotent(self, slot: tuple) -> tuple | None:
        rows = self.db.read("SELECT fingerprint, code, payload FROM idempotency WHERE merchant = ? AND caller = ? "
                            "AND path = ? AND key = ?", (self.merchant, *slot))
        return (rows[0][0], rows[0][1], json.loads(rows[0][2])) if rows else None

    def outbox_queue(self, name: str):
        from .webhooks import SqliteQueue
        return SqliteQueue(self.db, name)

    def catalog(self) -> list[dict] | None:
        """The imported catalog, or None if this merchant never imported one."""
        rows = self.db.read("SELECT data FROM catalog WHERE merchant = ? ORDER BY id", (self.merchant,))
        return [json.loads(r[0]) for r in rows] or None

    def save_catalog(self, products: list[dict]) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM catalog WHERE merchant = ?", (self.merchant,))
            for p in products:
                conn.execute("INSERT INTO catalog (merchant, id, data) VALUES (?, ?, ?)", (self.merchant, p["id"], _dump(p)))

    def remember(self, slot: tuple, fingerprint: str, code: int, payload: Any) -> None:
        self.db.write("INSERT INTO idempotency (merchant, caller, path, key, fingerprint, code, payload) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (merchant, caller, path, key) DO UPDATE SET "
                      "fingerprint = excluded.fingerprint, code = excluded.code, payload = excluded.payload",
                      (self.merchant, *slot, fingerprint, code, _dump(payload)))

    # read-only views, for tests and tools (each read is fresh from the database)
    @property
    def sessions(self) -> Mapping:
        return _Sessions(self)

    @property
    def orders(self) -> Sequence:
        return _Rows(self.db, "SELECT data FROM orders WHERE merchant = ? ORDER BY seq", (self.merchant,),
                     lambda row: json.loads(row[0]))

    @property
    def receipts(self) -> Sequence:
        return _Rows(self.db, "SELECT data FROM receipts WHERE merchant = ? ORDER BY seq", (self.merchant,),
                     lambda row: json.loads(row[0]))

    @property
    def audit(self) -> Sequence:
        return _Rows(self.db, "SELECT data FROM audit WHERE merchant = ? ORDER BY seq", (self.merchant,),
                     lambda row: json.loads(row[0]))


class _Sessions(Mapping):
    def __init__(self, repo: SqliteRepo):
        self.repo = repo

    def __getitem__(self, session_id):
        session = self.repo.session(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def __iter__(self):
        rows = self.repo.db.read("SELECT id FROM sessions WHERE merchant = ? ORDER BY length(id), id",
                                 (self.repo.merchant,))
        return iter([r[0] for r in rows])

    def __len__(self):
        return self.repo.db.read("SELECT COUNT(*) FROM sessions WHERE merchant = ?", (self.repo.merchant,))[0][0]


class _Rows(Sequence):
    def __init__(self, db: _Database, sql: str, args: tuple, make: Callable):
        self.db, self.sql, self.args, self.make = db, sql, args, make

    def _all(self) -> list:
        return [self.make(r) for r in self.db.read(self.sql, self.args)]

    def __getitem__(self, i):
        return self._all()[i]

    def __len__(self):
        return len(self.db.read(self.sql, self.args))

    def __iter__(self):
        return iter(self._all())

    def append(self, value):  # pragma: no cover - the merchant writes through the repo, never here
        raise TypeError("read-only view; write through the repository")


def _record_to_dict(record: dict) -> dict:
    return {**record, "charge": asdict(record["charge"])}


def _record_from_dict(d: dict) -> dict:
    from .psp import Charge
    return {**d, "charge": Charge(**d["charge"])}


# ---- the simulated processor's ledger ------------------------------------------------------------------


class LedgerCharges(Sequence):
    """``Processor.charges`` kept in SQLite: append records a charge, reads are fresh."""

    def __init__(self, db: _Database):
        self.db = db

    def append(self, charge) -> None:
        self.db.write("INSERT INTO ledger_charges (id, data) VALUES (?, ?)", (charge.id, _dump(asdict(charge))))

    def _all(self) -> list:
        from .psp import Charge
        return [Charge(**json.loads(r[0])) for r in self.db.read("SELECT data FROM ledger_charges ORDER BY seq")]

    def __getitem__(self, i):
        return self._all()[i]

    def __len__(self):
        return self.db.read("SELECT COUNT(*) FROM ledger_charges")[0][0]

    def __iter__(self):
        return iter(self._all())


class LedgerRefunds(Sequence):
    """``Processor.refunds`` kept in SQLite. Appending a refund also adds it to its charge's refunded total."""

    def __init__(self, db: _Database):
        self.db = db

    def append(self, refund) -> None:
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO ledger_refunds (data) VALUES (?)", (_dump(asdict(refund)),))
            row = conn.execute("SELECT data FROM ledger_charges WHERE id = ?", (refund.charge_id,)).fetchone()
            if row is not None:
                charge = json.loads(row[0])
                charge["refunded"] += refund.amount
                conn.execute("UPDATE ledger_charges SET data = ? WHERE id = ?", (_dump(charge), refund.charge_id))

    def _all(self) -> list:
        from .psp import Refund
        return [Refund(**json.loads(r[0])) for r in self.db.read("SELECT data FROM ledger_refunds ORDER BY seq")]

    def __getitem__(self, i):
        return self._all()[i]

    def __len__(self):
        return self.db.read("SELECT COUNT(*) FROM ledger_refunds")[0][0]

    def __iter__(self):
        return iter(self._all())
