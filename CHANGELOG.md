# Changelog

## 0.1.0 (unreleased)

### Added

- **Shopping assistant** (`shipjack/assistant`, `shipjack-assistant`): a LangGraph router and three agents
  (shopping, support, chat) with least privilege, human approvals for purchases (shopper) and refunds
  (store operator), model fallback, step budgets, an SSE API on Postgres (AsyncPostgresSaver), an event
  log, approval expiry, and the store's signed webhooks.
- **Web app** (`web/`, Next.js 16): a Liquid Glass shopper chat with an Apple Pay style approval sheet,
  and a store console (refunds, orders, catalog and knowledge, conversation replay, health).
- **Merchant onboarding and knowledge base**: Shopify export and CSV imports, policy pages, search by
  meaning on pgvector (vector, keyword or weighted hybrid), MCP `search_store_docs`, a retrieval eval
  (`shipjack retrieval-eval`, docs/RETRIEVAL.md). Orders keep the lines they were sold with.
- **Postgres store backend** with the same guarantees as SQLite (advisory lock), tested in CI.
- **Per-session locking and payments outside the transaction** (Postgres): a lock per checkout session
  instead of one for the whole store, blocking work in a bounded thread pool, the payment call made
  with no transaction open (mark the attempt, call the rail, record), reconciliation of attempts a
  crash left half done, 409 `payment_in_progress` / `idempotency_in_flight`, and an open-loop,
  multi-server mode for `shipjack load` (`--rate`, `--duration`, `--psp-delay-ms` on the server).
  Under a 300 ms payment API: 3 to about 60 purchases per second on three instances
  (docs/LOAD_RESULTS.md).
- **AP2 authority vectors for the fulfillment destination** (`shipjack ap2-vectors`,
  docs/AP2_FULFILLMENT_VECTORS.md): five cases in the two-record format of AP2 Discussion #332, run
  against the SDK at e1ea56d: a redirected ship-to and deliver-to (C4b), the same with the proposed
  fulfillment constraint, and a destination changed after the closed mandate was signed (C1, #358).
- **MCP over Streamable HTTP** at the store's `POST /mcp`.
- **MCP lost-response retry vectors** (`shipjack mcp-retry-vectors`, docs/MCP_RETRY_VECTORS.md): 16 cases
  for MCP issue #3394 (a response lost after the effect, then a retry with the same key, a new key or
  none; duplicates in flight; a restart; a server process killed after or before the charge; three
  processes at once), each with the ledger's count, the client's view and its conclusion.
- **One idempotency path for REST and MCP** (`shipjack/idempotency.py`): keys in the store's database,
  claimed under their own lock with the change; the payment mark carries the client's key, so whoever
  finishes a payment after a crash answers it. Sessions show `complete_in_progress` while the rail
  answers. A same-key retry in flight gets 409 `idempotency_in_flight` with `Retry-After`
  (ACP 2026-04-17).
- **Deployment**: a public demo mode (sandboxes, caps, no shared tokens), Dockerfiles, docker compose
  for the whole stack, a Render blueprint, docs/DEPLOY.md.

- **SQLite store** (`shipjack/store.py`, `shipjack-serve --db shop.db --workers N`, `build(..., db=)`):
  sessions, orders, receipts, the audit trail, idempotency answers, the webhook outbox, id counters,
  the checkout-signing key and the simulated processor's ledger in one WAL-mode file. Each merchant
  change is one `BEGIN IMMEDIATE` transaction shared by every server process; each POST's idempotency
  check, change and stored answer are one transaction. A failed change rolls back as a whole. The
  in-memory store stays the default, and every test that builds a store runs against both.
- **Transactional webhook outbox**: with a SQLite store, an event is written in the same transaction
  as its order change, survives a crash, is queued per destination URL, and is sent by one process at a
  time under a renewable lease that another process takes over when it expires.
- **Payment-rail idempotency keys**: each charge carries a key from the session, the attempt and the
  amount, and each refund one from the charge and what was already refunded; Stripe answers a repeated
  key with the first result (checked against Stripe's test API). A decline moves to a new key.
- **`shipjack load`**: drives a running store from several client processes (purchases and
  double-pay storms), reports throughput and latency, and counts checkouts charged more than once in
  the ledger. Results in docs/LOAD_RESULTS.md.
- `CheckoutError` and `PaymentError` can be pickled (they cross process boundaries).

- **Order webhooks** (`shipjack/webhooks.py`, `shipjack-serve --webhook-url`, `shipjack-receiver`):
  every order change goes to the agent platform as a signed ACP 2026-04-17 webhook (full Order
  snapshot; `Merchant-Signature: t=..,v1=..`, HMAC-SHA256 of `t.body`). An outbox retries network
  errors, 408, 425, 429 and 5xx with capped exponential backoff and the same `Request-Id`, dead-letters
  any other answer, and holds an order's later events behind a failed one. Plain HTTP only to
  localhost. The secret comes from `SHIPJACK_WEBHOOK_SECRET` (at least 32 bytes).
- **Reference receiver** (`create_receiver`): 401 `invalid_signature` for a missing, malformed, stale or
  wrong signature; drops duplicates by `Request-Id` and by signature (the `Request-Id` is not signed);
  never lets an older snapshot replace a newer one; accepts unknown event types and statuses.
- **ACP Order object and fulfillment** (`Merchant.acp_order`, `fulfill_order`, `mark_delivered`,
  `cancel_order`; `POST /orders/{id}/fulfill|deliver|cancel`): line items with
  `ordered`/`current`/`fulfilled` and derived status, shipping fulfillments with tracking and events,
  digital delivery, refunds as adjustments, `amount_refunded`. `GET /orders/{id}` now returns this
  object. Every webhook body is tested against the spec's schema (vendored in `tests/spec/`).
- **Operator tokens**: refund, fulfill, deliver, cancel and evidence need a merchant operator token
  (`create_app(..., operators=...)`, `SHIPJACK_OPERATOR_TOKEN`). Before, any agent platform token could
  refund an order.

- **Order lifecycle**: full and partial refunds (`Merchant.refund_order`, `POST /orders/{id}/refund`)
  on the rail that charged, including real Stripe Refunds in test mode; a merchant cannot refund more
  than it took, and concurrent refunds return the money once (locked in both the merchant and the
  processor). `GET /orders/{id}`.
- **Dispute evidence** (`Merchant.dispute_evidence`, `GET /orders/{id}/evidence`): the signed checkout,
  the mandate or approval reference, the receipt, the charge and its refunds, per AP2's dispute section.
- **Audit trail** (`Merchant.audit`): session created, checkout signed, order paid, order refunded.
- Direct (human-present) charges are now recorded with method `direct`, not `ap2`.

- **Runnable store** (`shipjack-serve`, `shipjack[serve]`): the ACP REST store over HTTP with a
  `/.well-known/acp.json` discovery document (transports, versions, payment handlers, extensions).
- **Stripe test-mode processor** (`shipjack/stripe_psp.py`, `shipjack[stripe]`): settles each order
  as a confirmed Stripe PaymentIntent in test mode, keeping every AP2 and card check. Refuses a
  non-test key; a decline leaves the order unpaid and the token unspent. `shipjack-serve --stripe`.
- **Deployment runbook** ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)): how a merchant adopts the
  integration.

- **Human-present (Direct) mode** (`shipjack/direct.py`): the user signs each purchase on a trusted
  surface after reading an approval screen. Shows that AP2's example screen omits the destination, so a
  careful user cannot catch a ship-to or gift-card-email redirect unless the screen is extended to show
  it. Merchant handler `direct`, a user-signed approval bound to the merchant, nonce, checkout and
  amount, single-use and expiring.
- **MCP store** (`shipjack/mcp.py`, `shipjack-mcp`): the store as an MCP server following the ACP MCP
  binding (five checkout tools, `{meta, id?, payload?}`, idempotency in `meta`), over a dependency-free
  JSON-RPC 2.0 stdio loop, plus discovery and product/AP2 extension tools. A purchase over MCP reaches
  the same outcome as over the in-process API.

### Hardening (found by the edge-case tests)

- Mailboxes in the fulfillment check were compared with every dot removed, so a mailbox on another
  domain could pass (`bob@my.co.uk` and `bob@myco.uk` compared equal). They are now compared exactly
  after Unicode compatibility normalisation and case folding. A list, a display name or angle brackets
  match nothing.
- Address fields the check does not know (`line_three`, `care_of`, `instructions`), non-text values and
  non-object addresses are refused instead of ignored. Non-US postal codes are compared in full.
- Malformed API requests are flat 4xx errors, not 500s. An unexpected fault is a flat 500 that leaks
  nothing and is never cached against the `Idempotency-Key`. One order may ask for at most 1000 of an
  item, the same product on two lines is merged, and a create needs `items`.
- Check-then-act steps (redeeming a token, spending a nonce, completing a session) run under locks.
  Twelve threads redeeming one token used to charge it more than once.
- Open mandates must expire, and within 24 hours. The wallet with the extensions refuses a signed
  checkout whose totals do not add up.
- 276 tests: every AP2 MUST around mandate chains, awkward ACP input, matching corner cases,
  concurrency, and seeded fuzzing.

First version.

- Merchant: ACP Agentic Checkout sessions (create, update, get, complete, cancel) in-process and as a
  FastAPI REST API with `API-Version`, bearer auth, `Idempotency-Key` replay and conflict rules, and
  flat errors. AP2 v0.2 additions: a merchant-signed UCP Checkout JWT with a single-use nonce per
  approval, Checkout Mandate verification against the latest checkout, signed receipts, and a
  published checkout key.
- Payment side: a credential provider that releases a single-use token only for a verified Payment
  Mandate inside its constraints, and a processor that pays out only for the token's checkout, amount
  and payee, with a ledger as ground truth.
- Trusted surface and wallet: open Checkout and Payment Mandates signed from the user's intent; a
  deterministic wallet that closes them, follows AP2's no-overlapping-mandates rule, and (with the
  extensions) checks the merchant's published key and every constraint before the agent key signs.
- Extension: `io.github.srivatsa03.fulfillment`, a checkout constraint on where goods go (ship-to
  address, delivery email). Verifiers that do not know it fail closed.
- Bench: 12 scenarios (3 normal purchases, 9 attacks). `shipjack bench` runs a fully fooled
  scripted agent; `shipjack llm-bench` puts a real model in the agent's seat with paired runs
  across profiles, scored from the processor ledger and orders; `--rescore` rebuilds the tables;
  `shipjack demo` tells one scenario step by step.
- Real-model results for gpt-oss-120b (Groq), gemini-3.1-flash-lite-preview and gemini-3.8-flash
  (Gemini API): 45 paired trials each in `docs/LLM_RESULTS.md`, and a delivery study (5 variants x 10
  trials) in `docs/DELIVERY_RESULTS.md`, with every transcript in the matching `.json` files.
- Three more delivery attacks: the shipping and gift-card notices posted as customer reviews, and a
  quiet same-city courier address.
- `--provider gemini` (Gemini's OpenAI-compatible endpoint).
- A trial in which the provider fails (an API error, as opposed to the model running out of steps) is
  discarded and run again on resume, for every profile, so a rate limit never counts as a result.
- Built on Google's AP2 SDK (pinned to e1ea56d, AP2 v0.2) and the ACP spec at 2026-04-17.

### Fixed

- MCP idempotency keys lived in each server process's memory and were checked without a claim: 8
  identical creates at once made 8 sessions, three processes made three, and a restart forgot every key.
  The request digest included `meta`, so a retry with a new `request_id` was refused as a conflict. A
  read-back during a payment said `ready_for_payment`. The REST API stored `payment_in_progress` as a
  key's answer. The MCP endpoint ran its work on the event loop. (Found by the MCP retry vectors.)
- A session could be canceled while its payment was in flight; an order could record the catalog's
  prices at the end of a payment instead of what was charged; a charge's ledger write could deadlock
  with a refund on SQLite (all found while splitting the payment out of the transaction).
