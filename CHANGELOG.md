# Changelog

## 0.1.0 (unreleased)

### Added

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
