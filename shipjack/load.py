"""Load-test a running shipjack store over HTTP, and check the money afterwards.

    shipjack-serve --db shop.db --workers 4 &
    shipjack load --url http://127.0.0.1:8000 --purchases 5000 --concurrency 32 --clients 4 --db shop.db

``--url`` takes several comma-separated servers sharing one store (separate instances behind no load
balancer): client processes and the agents of each storm are spread across them.

Two workloads:

* **Purchases.** Many agents at once, each doing a full ACP purchase (create a checkout session, then
  complete it with a card). Reported as purchases per second and latency percentiles per purchase.
  Closed loop by default (``--concurrency`` agents in flight per client). With ``--rate`` it is open
  loop instead: purchases start on a fixed schedule whatever the server does, and each latency is
  measured from the scheduled start, so a server that falls behind shows its queue in the latency
  instead of quietly slowing the load down (coordinated omission).
* **Double-pay storms.** For each storm, one checkout session and ``--storm-size`` agents paying it at
  the same moment, each with its own ``Idempotency-Key`` (so idempotency cannot hide a race). Exactly
  one of them may be charged.

The client runs in several processes, because one Python process tops out before the server does.
With ``--db`` (the store's SQLite file) it then reads the processor ledger and counts any checkout
charged more than once. That count, not the HTTP answers, is the double-charge number.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
import uuid
from concurrent.futures import ProcessPoolExecutor

from .merchant import ACP_API_VERSION

ADDRESS = {"name": "Sam Rivera", "line_one": "1200 W Harrison St", "city": "Chicago", "state": "IL",
           "postal_code": "60607", "country": "US"}
CARD = {"payment_data": {"handler_id": "card",
                         "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}}
CART = {"items": [{"id": "nw-hub-7", "quantity": 1}], "fulfillment_details": {"address": ADDRESS}}


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "API-Version": ACP_API_VERSION, "Content-Type": "application/json"}


async def _post(client, path: str, body: dict, token: str):
    return await client.post(path, json=body, headers={**_headers(token), "Idempotency-Key": str(uuid.uuid4())})


async def _purchases(url: str, n: int, concurrency: int, token: str) -> dict:
    import httpx
    latencies, errors = [], []
    gate = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, limits=limits, timeout=60) as client:
        async def one():
            async with gate:
                start = time.perf_counter()
                try:
                    session = await _post(client, "/checkout_sessions", CART, token)
                    done = await _post(client, f"/checkout_sessions/{session.json()['id']}/complete", CARD, token)
                    if done.status_code != 200 or done.json().get("status") != "completed":
                        errors.append(f"HTTP {done.status_code}")
                        return
                except Exception as exc:  # a client-side failure is an error, never a silent pass
                    errors.append(type(exc).__name__)
                    return
                latencies.append((time.perf_counter() - start) * 1000)
        await asyncio.gather(*[one() for _ in range(n)])
    return {"latencies": latencies, "errors": errors}


async def _scheduled(url: str, rate: float, duration: float, token: str, timeout: float) -> dict:
    """Open loop: start a purchase every 1/rate seconds for ``duration``; latency counts from the schedule."""
    import httpx
    latencies, errors = [], []
    limits = httpx.Limits(max_connections=2000, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=url, limits=limits, timeout=timeout) as client:
        async def one(due: float):
            try:
                session = await _post(client, "/checkout_sessions", CART, token)
                done = await _post(client, f"/checkout_sessions/{session.json()['id']}/complete", CARD, token)
                if done.status_code != 200 or done.json().get("status") != "completed":
                    errors.append(f"HTTP {done.status_code}")
                    return
            except Exception as exc:  # noqa: BLE001 - a timeout here is the server falling behind: an error, never a pass
                errors.append(type(exc).__name__)
                return
            latencies.append((time.perf_counter() - due) * 1000)
        t0, tasks = time.perf_counter(), []
        for k in range(int(rate * duration)):
            due = t0 + k / rate
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            tasks.append(asyncio.create_task(one(due)))
        await asyncio.gather(*tasks)
    return {"latencies": latencies, "errors": errors}


def _client(url: str, n: int, concurrency: int, token: str, start: float, rate: float = 0.0,
            duration: float = 0.0, timeout: float = 60.0) -> dict:
    time.sleep(max(0.0, start - time.time()))
    began = time.time()
    if rate:
        out = asyncio.run(_scheduled(url, rate, duration, token, timeout))
    else:
        out = asyncio.run(_purchases(url, n, concurrency, token))
    return {**out, "began": began, "ended": time.time()}


async def _storms(urls: list[str], storms: int, size: int, token: str) -> list[dict]:
    """Each storm: one session, ``size`` agents paying it at once, spread over every server."""
    import httpx
    limits = httpx.Limits(max_connections=size, max_keepalive_connections=size)
    out = []
    clients = [httpx.AsyncClient(base_url=u, limits=limits, timeout=60) for u in urls]
    try:
        for _ in range(storms):
            session = (await _post(clients[0], "/checkout_sessions", CART, token)).json()["id"]
            answers = await asyncio.gather(*[_post(clients[i % len(clients)], f"/checkout_sessions/{session}/complete",
                                                   CARD, token) for i in range(size)])
            paid = sum(1 for a in answers if a.status_code == 200 and a.json().get("status") == "completed")
            out.append({"session": session, "paid_answers": paid,
                        "server_errors": sum(1 for a in answers if a.status_code >= 500)})
    finally:
        for c in clients:
            await c.aclose()
    return out


def ledger(db: str, sessions: list[str] | None = None) -> dict:
    """Count charges per checkout in the processor ledger (a SQLite path or a Postgres URL). Read-only."""
    from collections import Counter

    from .store import open_database
    database = open_database(db)
    try:
        with database.transaction(write=False):  # one snapshot: charges and orders counted at the same moment
            per = Counter(json.loads(row[0]).get("reference") for row in database.read("SELECT data FROM ledger_charges"))
            orders = database.read("SELECT COUNT(*) FROM orders")[0][0]
    finally:
        database.close()
    out = {"charges": sum(per.values()), "orders": orders,
           "checkouts_charged_more_than_once": sum(1 for c in per.values() if c > 1)}
    if sessions is not None:
        out["storm_sessions_charged_exactly_once"] = sum(1 for s in sessions if per.get(s) == 1)
    return out


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 1) if ordered else 0.0


def run(url: str, purchases: int, concurrency: int, clients: int, storms: int, storm_size: int,
        token: str = "demo-token", db: str | None = None, rate: float = 0.0, duration: float = 0.0,
        timeout: float = 60.0) -> dict:
    urls = [u.strip() for u in url.split(",") if u.strip()]
    report: dict = {"url": url, "servers": len(urls), "clients": clients}
    if rate:
        report.update(mode="open loop", offered_per_second=rate, duration_s=duration, timeout_s=timeout)
    else:
        report.update(mode="closed loop", concurrency_per_client=concurrency)
    if purchases or rate:
        per, extra = divmod(purchases, clients)
        start = time.time() + 5  # every client process starts at the same moment, after its imports
        with ProcessPoolExecutor(clients, mp_context=multiprocessing.get_context("spawn")) as pool:
            parts = [f.result() for f in [pool.submit(_client, urls[i % len(urls)], per + (1 if i < extra else 0),
                                                      concurrency, token, start, rate / clients, duration, timeout)
                                          for i in range(clients)]]
        wall = max(p["ended"] for p in parts) - min(p["began"] for p in parts)
        latencies = [x for p in parts for x in p["latencies"]]
        errors = [e for p in parts for e in p["errors"]]
        attempted = int(rate / clients * duration) * clients if rate else purchases
        report["purchases"] = {"attempted": attempted, "completed": len(latencies), "errors": len(errors),
                               "error_kinds": sorted(set(errors))[:5], "wall_s": round(wall, 2),
                               "per_second": round(len(latencies) / wall, 1) if wall > 0 else 0.0,
                               "p50_ms": _pct(latencies, .50), "p95_ms": _pct(latencies, .95),
                               "p99_ms": _pct(latencies, .99)}
    sessions = None
    if storms:
        results = asyncio.run(_storms(urls, storms, storm_size, token))
        sessions = [r["session"] for r in results]
        report["storms"] = {"storms": storms, "agents_per_storm": storm_size,
                            "storms_with_one_paid_answer": sum(1 for r in results if r["paid_answers"] == 1),
                            "most_paid_answers_in_a_storm": max(r["paid_answers"] for r in results),
                            "server_errors": sum(r["server_errors"] for r in results)}
    if db:
        report["ledger"] = ledger(db, sessions)
    return report


def add_parser(sub) -> None:
    p = sub.add_parser("load", help="load-test a running store over HTTP and check the ledger for double charges")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="a server, or several comma-separated")
    p.add_argument("--purchases", type=int, default=2000)
    p.add_argument("--concurrency", type=int, default=32, help="agents in flight per client process")
    p.add_argument("--clients", type=int, default=4, help="client processes")
    p.add_argument("--storms", type=int, default=50, help="double-pay storms (0 to skip)")
    p.add_argument("--storm-size", type=int, default=20, help="agents paying one session at once")
    p.add_argument("--rate", type=float, default=0.0, help="open loop: purchases started per second, in total")
    p.add_argument("--duration", type=float, default=30.0, help="open loop: seconds to keep starting purchases")
    p.add_argument("--timeout", type=float, default=60.0, help="seconds before a request counts as an error")
    p.add_argument("--token", default="demo-token")
    p.add_argument("--db", help="the store's SQLite file or Postgres URL, to count charges per checkout in the ledger")
    p.add_argument("--json", help="also write the report here")
    p.set_defaults(func=main)


def main(args) -> int:
    report = run(args.url, args.purchases, args.concurrency, args.clients, args.storms, args.storm_size,
                 args.token, args.db, args.rate, args.duration, args.timeout)
    text = json.dumps(report, indent=1)
    print(text)
    if args.json:
        with open(args.json, "w") as f:
            f.write(text + "\n")
    bad = report.get("purchases", {}).get("errors", 0) or report.get("storms", {}).get("server_errors", 0)
    doubles = report.get("ledger", {}).get("checkouts_charged_more_than_once", 0)
    return 1 if bad or doubles else 0
