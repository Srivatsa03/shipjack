# Load and failure results

What the store does under load, when processes race, and when a process dies. Raw numbers are in
[load_results.json](load_results.json).

## Setup

- **Machine:** one Apple M1 laptop (8 cores, 8 GB). The load generator runs on the same machine, so
  the server and the clients compete for the same cores.
- **Server:** `shipjack-serve --db shop.db --workers N`: uvicorn, the state in one SQLite file (WAL),
  the simulated processor (no Stripe calls under load).
- **Load:** `shipjack load --purchases 10000 --concurrency 16 --clients 4 --storms 200 --storm-size 20 --db shop.db`
  - 10,000 full purchases (create a checkout session, then pay it), from 4 client processes with 16
    agents in flight each.
  - 200 double-pay storms: for each, one checkout session and 20 agents paying it at the same moment,
    each with its own `Idempotency-Key`, so idempotency cannot hide a race.
  - The double-charge count comes from the processor ledger in the database, not from the HTTP answers.

## Results

| Server processes | Purchases | Errors | Purchases/s | p50 | p95 | p99 | Storms charged exactly once | Checkouts charged twice |
|---|---|---|---|---|---|---|---|---|
| 1 | 10,000 | 0 | 402 | 128 ms | 325 ms | 459 ms | 200 of 200 | 0 |
| 2 | 10,000 | 0 | 448 | 113 ms | 319 ms | 476 ms | 200 of 200 | 0 |
| 4 | 10,000 | 0 | 583 | 75 ms | 296 ms | 507 ms | 200 of 200 | 0 |
| 6 | 10,000 | 0 | 645 | 61 ms | 266 ms | 439 ms | 200 of 200 | 0 |

Across the four runs: 40,000 purchases and 16,000 racing payment attempts (800 storms of 20), 0
errors, and the ledger held exactly one charge per order (10,200 charges for 10,200 orders in each run).

The latency is per purchase, two HTTP calls. Throughput is limited by this laptop running the load
generator next to the server, not by a measured server ceiling. SQLite admits one writer at a time, so
more processes help with the HTTP and JSON work, not with the writes.

## Racing processes (tests/test_store.py)

Real OS processes, each with its own store object on one SQLite file, released at the same instant:

- 8 processes paying one checkout session: one charge, one order.
- 8 processes refunding one order in full: one refund.
- 4 processes creating 25 sessions each: 100 distinct ids.
- 4 processes shopping and sending webhooks through one shared outbox: 60 events, each delivered
  once, every order's events in order.

With the database transaction switched off, the first two tests fail (two charges, two refunds), so
they test the lock and not luck.

## A process killed mid-delivery

1. Store running with `--db` and `--webhook-url`, receiver down. Three purchases, each shipped: six
   events pending, each tried twice.
2. `kill -9` the store.
3. Start the receiver, restart the store on the same file.
4. All six events delivered, each order's "created" before its "shipped"; replaying the first payment's
   request returned the stored answer (`Idempotent-Replayed: true`); the ledger held 3 charges for 3
   orders.

## A crash between charging and recording (tests/test_store.py, live Stripe check)

The payment and the order commit in one transaction, so a crash after the payment rail charged but
before the order was recorded rolls the order back. The retry must not charge again. Every charge and
refund carries an idempotency key built from the checkout session, the attempt and the amount, and
Stripe answers a repeated key with the first result. Checked against Stripe's test API: the same key
returned the same PaymentIntent, and a new key made a new one
(`test_live_stripe_returns_the_first_payment_for_a_repeated_idempotency_key`). A declined attempt
moves to a new key, so the user can retry with another card.

## Limits

- One machine. SQLite is the right tool for one host; several hosts need a server database (row locks
  in Postgres, the same idempotency keys).
- The Stripe call runs inside the write transaction. That is what makes "charge and record" atomic
  here, but it holds the writer lock for the length of a Stripe round trip. A high-volume deployment
  would split it: reserve the attempt, charge with the idempotency key outside the lock, then record.
- If the process dies after the rail charged and the client never retries, the charge has no order.
  Finding those (a PaymentIntent with this store's metadata and no order) is a reconciliation job this
  project does not build.
