# Changelog

## 0.1.0 (unreleased)

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
- Bench: 9 scenarios (3 normal purchases, 6 attacks). `agent-checkout bench` runs a fully fooled
  scripted agent; `agent-checkout llm-bench` puts a real model in the agent's seat with paired runs
  across profiles, scored from the processor ledger and orders; `--rescore` rebuilds the tables;
  `agent-checkout demo` tells one scenario step by step.
- Built on Google's AP2 SDK (pinned to e1ea56d, AP2 v0.2) and the ACP spec at 2026-04-17.
