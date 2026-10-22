# MCP lost-response retry vectors: a checkout store

Runnable cases for [MCP issue #3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394):
a mutating `tools/call` commits its effect, the response is lost, and the client retries with a fresh
JSON-RPC id. They run against a real store: shipjack's MCP endpoint (Streamable HTTP, JSON answers), the
ACP checkout tools, a SQLite database and the simulated payment processor. Each case keeps three records
apart, as the shared fixture in
[arjun2075/mcp-b1-lost-response-repro#2](https://github.com/arjun2075/mcp-b1-lost-response-repro/pull/2) does:

- **Effects**: what the processor's ledger (or the session table) says happened. This is the oracle.
- **Observed**: what the client saw on each call.
- **Reported**: what the client can conclude, from the retry's answer alone, and after reading the
  session back with `get_checkout_session`.

```bash
shipjack mcp-retry-vectors --json docs/mcp_retry_vectors.json   # about 10 s; exits 1 if any case fails
```

Raw results, with every call's answer: [mcp_retry_vectors.json](mcp_retry_vectors.json). The same
cases run as tests (`tests/test_mcp_retry_vectors.py`), plus a concurrency test on every backend,
including Postgres.

**Faults.** In the L cases the server commits and answers, and the client drops the answer (it sees a
timeout). In the K cases the store runs in a child process that ends itself with `os._exit(137)` right
after the rail charged (K1) or right before it would have (K2); the client's connection drops, and a new
process starts on the same database. One scripted application retry per step, never an SDK retry. No
model is involved.

**What carries the identity.** `complete_checkout_session` names a checkout session that the server
issued in an earlier call, and a session is paid at most once. So a retry with a new key, or none, cannot
pay twice: the key only decides whether the retry gets the original answer back. `create_checkout_session`
has no such identity, so without a key a retry makes a second session (no money moves at create).

## Results (October 21 2026)

| ID | Shared matrix | Case | Effects | Client concludes | As expected |
|---|---|---|---|---|---|
| L1 | B02, R01, ryjen B | complete: response lost, retry with the same key | 1 charge | succeeded (the stored answer) | yes |
| L2 | ryjen A | complete: response lost, retry with a new key | 1 charge | answer alone: **failed**; read-back: succeeded | yes |
| L3 | ryjen E | complete: response lost, retry with no key | 1 charge | answer alone: **failed**; read-back: succeeded | yes |
| L4 | B01 shape, ryjen A | create: response lost, retry with no key | **2 sessions** | a new session id | yes |
| L5 | B02, ryjen B | create: response lost, retry with the same key | 1 session | the first session id | yes |
| L6 | ryjen D, T04 | create: the same cart again on purpose, new key | 2 sessions | a new session id | yes |
| L7 | harness requirement 2 | same key, a new `meta.request_id` on the retry | 1 charge | succeeded | yes |
| L8 | A06 | same key, different arguments | 1 charge | `idempotency_conflict`, nothing ran | yes |
| L9 | A05 | another caller sends the same key for its own session | 1 charge each, no receipt crosses | succeeded | yes |
| L10 | X02, ryjen C | the rail is still answering (600 ms) when the client asks again | 1 charge | unknown while in flight (answer and read-back); succeeded after | yes |
| L11 | X01, C01 | 8 identical creates, then 8 identical completes, at once | 1 session, 1 charge | in this run 1 completed, 7 `idempotency_in_flight`; then the stored answer | yes |
| L12 | durable recovery | answers lost, server restarted before each retry | 1 session, 1 charge | succeeded | yes |
| L13 | stored failure | a declined card; retry, then a good card with a new key | 1 charge | the same decline; then succeeded | yes |
| K1 | R03 | the process dies after the rail charged, before the order | 1 charge | unknown at once (`idempotency_in_flight`, read-back `complete_in_progress`); succeeded once reconciled | yes |
| K2 | R02, then non-execution | the process dies after marking the attempt, before the rail call | 1 charge | unknown at once; succeeded once the attempt was stale and freed | yes |
| K3 | C01, X01 across processes | 9 identical creates, then 9 identical completes, over 3 server processes on one database (300 ms rail) | 1 session, 1 charge | in this run 1 completed, 8 `idempotency_in_flight`; then the stored answer | yes |

## What the cases found in shipjack, and what changed

Run against the code before the changes below (d8b3073, with this module added), 6 of the 16 cases
failed ([mcp_retry_vectors_before.json](mcp_retry_vectors_before.json)). No case charged twice, even then:
the checkout session protected the money. What failed was everything around it.

| Case | Before | Cause |
|---|---|---|
| L11 | 8 identical creates made **8 sessions** | MCP keys were checked in memory, with no claim, so concurrent duplicates all ran |
| K3 | 9 identical creates over 3 processes made **3 sessions** | MCP keys lived in each server process's memory |
| L12 | after a restart the create retry made a **second session**, and the complete retry was told **failed** | the same: a restart forgot every MCP key |
| L7 | a retry with a new `meta.request_id` got `idempotency_conflict`, so the client reported **failed** | the request digest included `meta` |
| L10 | while the rail was answering, the read-back said `ready_for_payment`: **not paid** | the session showed no in-flight state |
| K2 | after the crash the read-back said `ready_for_payment`: **not paid**, while the attempt was unresolved | the same |

The changes (all in this commit):

- **One idempotency path for REST and MCP** (`shipjack/idempotency.py`). Keys live in the store's
  database, scoped to the authenticated caller and the operation, and are claimed under their own lock
  in the same transaction as the change. The request digest leaves `meta` out, as REST leaves headers out.
  A retry while the first request is running gets `idempotency_in_flight` with `Retry-After`
  (ACP 2026-04-17); a refusal that only says "still running" is not stored and does not free the key.
- **The payment mark carries the client's key.** If the process dies between charging and recording,
  whoever finishes the attempt (a retry with another key, or reconciliation) stores the answer under the
  original key, so its retry gets the receipt (K1). If the attempt is freed instead, the key is freed
  with it, so the retry pays once (K2).
- **`complete_in_progress`** (an ACP 2026-04-17 session status) while a payment is in flight, so a
  read-back never says "not paid" while the rail may be charging.
- The MCP endpoint's work now runs in the thread pool like every other route; before, one slow
  payment over MCP held up every request in that process.

Each change has a test that fails without it, checked by removing each piece in turn: 11 of 11 caught
(the key's own lock only shows on Postgres, where creates share no other lock). Before the thread-pool
change, a discovery request to the same process waited 1.21 s behind a 1.5 s MCP payment; after it,
0.01 s.

## What the cases suggest for the retry-identity SEP

These are observations from one implementation, not proposals:

1. **A server-issued operation identity made the money safe without any key.** ACP splits a purchase
   into create (returns a session id) and complete (names it), and a session is paid once. L2 and L3 retry
   with a new key or none and still charge once. The key's job shrinks to returning the original answer.
2. **The answer alone can say "failed" for an operation that succeeded** (L2, L3: `session is already
   completed`). Only the read-back shows the truth, and only because the session state is authoritative.
3. **A read-back needs an in-flight state to be trusted.** Before the fix, a read-back during the charge
   said "not paid" (L10, K2), which is exactly the X02 trap. `complete_in_progress` turns it into "unknown".
4. **The key's answer has to be written by whoever finishes the effect,** not by the request that
   started it, or a crash leaves the key claimed with no answer (K1). This is the recovery-point
   pattern (Brandur Leach's atomic phases) with the client's key added to the recovery point.
5. **"Still running" is not an answer.** The REST API used to store `payment_in_progress` as a second
   key's answer, so that key replayed "in progress" after the payment had finished. And a first version
   of this change freed the first request's key when a retry was told "in flight", so a third retry ran.
   Both are fixed, and tests catch both.

## Limits

- One machine. The cases use SQLite (WAL); the unit tests also run on Postgres. The simulated processor
  has fixed delays; a real payment API varies and fails in other ways.
- Faults are injected: the client drops an answer it received, or the server ends itself with
  `os._exit`. No real network partition.
- K2 treats an attempt as stale after 1.5 s so the case runs quickly; the default is 120 s, and it must
  stay above the payment API's own timeout. With Stripe, reconciliation first resends the same payment
  key (tested separately, including against Stripe's test API), so a charge that did reach Stripe is
  found, not repeated.
- The ACP MCP binding makes `meta.idempotency_key` optional (ACP issue #295), and so does this store,
  which is why L4 makes two sessions.
- A same-key retry right after a crash waits for reconciliation (`SHIPJACK_RECONCILE_EVERY_S`, 30 s by
  default) or for a retry with another key; it is told `idempotency_in_flight` until then.
