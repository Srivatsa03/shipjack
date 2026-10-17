# Load and failure results

What the store does under load, when processes race, and when a process dies. Raw numbers are in
[load_results_slow_psp.json](load_results_slow_psp.json) (a slow payment API, Postgres) and
[load_results.json](load_results.json) (an instant payment API, SQLite).

## A slow payment API (Postgres, 3 instances), October 17 2026

A real payment API answers in hundreds of milliseconds. With the simulated processor set to take
300 ms per charge (`--psp-delay-ms 300`), three store instances sharing one Postgres database
topped out at **3 purchases per second**: 1 ÷ 0.3 s. The cause was the store's design, not the laptop:

1. **One lock for the whole store.** Every write took the same Postgres advisory lock, so every
   instance waited for every other.
2. **The payment call inside that lock.** The rail call ran inside the write transaction, so a
   300 ms payment held the whole store for 300 ms.
3. **Blocking work on the event loop.** The HTTP handlers were `async` but ran their database and
   payment work directly on the event loop, so each process served one request at a time.
4. **The processor's lock held across the payment call.**

What changed:

- **Per-session locks.** A transaction names what it locks (`transaction(keys=...)`): the store lock
  in shared mode plus an exclusive advisory lock per checkout session (an order shares its session's
  lock). Changes to different sessions run in parallel; anything not split by key (a catalog import)
  still takes the store lock exclusively and waits for all of them. Objects are written back only if
  they changed, and a keyed transaction that changes a session it did not lock fails instead of
  overwriting it. BEGIN and the locks go in one round trip.
- **Blocking work in a bounded thread pool** (`SHIPJACK_THREADS`, default 16 per process), each thread
  with its own connection.
- **The payment call outside any transaction**, in three steps: mark the attempt (with its idempotency
  key and the lines and amount being charged), call the rail with no transaction open, then record the
  order or the decline. Another request for the session meanwhile gets 409 `payment_in_progress`; a
  second request with the same `Idempotency-Key` gets 409 `idempotency_key_in_use`. The HTTP layer's
  idempotency record rides in the first and last of those transactions.
- **Reconciliation.** If a process dies between the steps, the attempt stays marked. The next request
  for the session, or a timer in every server process (`SHIPJACK_RECONCILE_EVERY_S`), finds the charge
  by its idempotency key and records the order; an attempt older than 120 s with no charge anywhere is
  freed. With Stripe it asks again with the same key, which returns the first PaymentIntent.

Open-loop load (purchases start on a schedule; latency counts from the scheduled start), 30 s per
rate, the same laptop, script and settings for every version:

| Offered | Before: done/s | p99 | Per-session locks: done/s | p99 | + payment outside the transaction: done/s | p99 |
|---|---|---|---|---|---|---|
| 4/s | 3.0 | 22.5 s | 4.1 | 0.42 s | 4.1 | 0.46 s |
| 8/s | 3.0 (62 timeouts) | 30.8 s | 8.0 | 0.45 s | 8.0 | 0.39 s |
| 16/s | 2.9 (302 timeouts) | 37.6 s | 15.9 | 0.58 s | 15.9 | 0.56 s |
| 32/s | – | – | 31.7 | 1.17 s | 31.6 | 0.89 s |
| 64/s | – | – | 63.2 | 3.37 s | 45.1 | 18.6 s |
| 128/s | – | – | 54.3 (overloaded) | 40.5 s | 43.4 (overloaded) | 56.1 s |

Every run also raced 50 storms of 20 agents paying one session across the three servers: each storm
session was charged exactly once. Ledger totals at the end: 586 charges for 586 orders (before),
7,610 for 7,610 (per-session locks), 7,574 for 7,574 (payment outside the transaction), 0 checkouts
charged twice.

What the numbers say:

- **Per-session locks and the thread pool** take three instances from 3 to about 60 purchases per
  second under a 300 ms payment API, about 20 times more.
- **Moving the payment call out of the transaction** does not add throughput here. Its tail latency is
  a little better at 32/s, and its peak is lower (about 45/s against about 60/s), because it makes more
  database round trips per purchase (about 16 against about 12) and each costs about 16 ms between this
  laptop and the Postgres VM. Its value is elsewhere: no transaction or lock is held while the rail
  answers (a test checks it, directly and over HTTP), so a slow or hanging payment provider cannot pile
  up open transactions, and a crash between charging and recording is finished by reconciliation
  instead of waiting for a client retry.
- The first version of that change took about 29 round trips per purchase and peaked at about 38/s;
  folding the idempotency record into the payment transactions and sending BEGIN with its locks
  brought it to about 16.
- At 48 threads per process both versions collapse (about 15/s, about 145 connections in open
  transactions at once): Postgres in a 4-CPU VM and seven Python processes saturate this 8-core
  laptop. That is this machine's database limit, not a result about either design.

## An instant payment API (SQLite), October 16 2026

Measured before the changes above (one store-wide writer), with the simulated processor settling
instantly.

### Setup

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

### Results

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

Real OS processes, each with its own store object on one SQLite file or Postgres schema, released at
the same instant:

- 8 processes paying one checkout session: one charge, one order. (More than one may answer
  "completed": a racer that finds the charge already on record finishes the order from it. The ledger,
  not the HTTP answers, is the double-charge count.)
- 8 processes refunding one order in full: one refund.
- 4 processes creating 25 sessions each: 100 distinct ids.
- 4 processes shopping and sending webhooks through one shared outbox: 60 events, each delivered
  once, every order's events in order.

With the database transaction switched off, the first two tests fail (two charges, two refunds), so
they test the lock and not luck. On Postgres they also fail when a session change takes no session lock.
Other tests break one guard each and fail without it: a slow payment on one session must not hold up
another; a catalog import waits for every keyed transaction; a keyed change cannot rewrite a session
it did not lock; nothing changes a session while its payment is in flight; the order records the
lines priced when the attempt began; the processor's lock is never held during a ledger write (a
deadlock against a refund on SQLite); no transaction is open during the rail call.

## A process killed mid-delivery

1. Store running with `--db` and `--webhook-url`, receiver down. Three purchases, each shipped: six
   events pending, each tried twice.
2. `kill -9` the store.
3. Start the receiver, restart the store on the same file.
4. All six events delivered, each order's "created" before its "shipped"; replaying the first payment's
   request returned the stored answer (`Idempotent-Replayed: true`); the ledger held 3 charges for 3
   orders.

## A crash between charging and recording (tests/test_store.py, live Stripe check)

The rail call runs outside any transaction, so once it returns the money has moved. Every charge and
refund carries an idempotency key built from the checkout session, the attempt and the amount, and the
ledger records it with the charge. The tests kill the payment at each point:

- **After the rail charged and the ledger recorded it, before the order:** the next request for the
  session finds the charge by its key and records the order; Stripe is not called again.
- **After Stripe charged, before anything was recorded:** a retry is told the payment is in progress;
  once the attempt is stale, reconciliation sends the same request with the same key, Stripe answers
  with the first PaymentIntent, and the order is recorded once.
- **Before the rail answered:** once stale, reconciliation frees the session, and the next attempt
  pays with a new key.

Stripe's behaviour was checked against its test API: the same key returned the same PaymentIntent and a
new key made a new one (`test_live_stripe_returns_the_first_payment_for_a_repeated_idempotency_key`). A
declined attempt moves to a new key, so the user can retry with another card.

## Limits

- One laptop for everything: the store, the load generator and Postgres (in a VM, about 16 ms per round
  trip). The comparisons hold because every version ran on the same machine with the same script; the
  absolute numbers are this laptop's.
- The simulated processor stands in for the payment API (a fixed 300 ms); a real one varies, times out
  and fails in ways a fixed delay does not.
- An attempt is treated as dead after 120 s with no charge on record. A payment call that is somehow
  still running after that, and then succeeds, would leave a charge with no order; the timeout must stay
  above the rail's own timeout.
- Asking Stripe again after a crash makes the payment if the first request never reached Stripe: it
  completes the payment the user approved, once, rather than abandoning it.
- Refunds still call the rail inside the order's transaction (with only that order locked); they are
  rare next to purchases.
