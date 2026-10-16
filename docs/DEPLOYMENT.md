# Deploying shipjack for a merchant

This is the integrator's runbook: how a merchant goes from "I want to accept orders from AI agents"
to a running, agent-ready store. shipjack is a reference integration, not a production service; this
doc is written as the guide an FDE would hand a merchant, and it marks what is real versus simulated.

## 1. What you get

- An ACP checkout API over REST, and the same store as an MCP server.
- AP2 v0.2 payment verification (human-not-present and human-present).
- A payment rail: Stripe test mode, or a simulated processor.
- A discovery document at `/.well-known/acp.json` so agents can find the transports and versions.

## 2. Prerequisites

- Python 3.11 or 3.12.
- `pip install -e ".[dev,serve]"` (add `stripe` for the real rail: `pip install -e ".[stripe]"`).

## 3. Run the store

```bash
shipjack-serve --host 0.0.0.0 --port 8000
```

Check discovery (no auth needed):

```bash
curl -s http://127.0.0.1:8000/.well-known/acp.json
```

An agent platform authenticates with a bearer token (one per platform; `--token` sets the demo one):

```bash
curl -s -X POST http://127.0.0.1:8000/checkout_sessions \
  -H "Authorization: Bearer demo-token" -H "API-Version: 2026-04-17" \
  -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"items":[{"id":"nw-hub-7","quantity":1}],
       "fulfillment_details":{"address":{"name":"Sam","line_one":"1200 W Harrison St",
         "city":"Chicago","state":"IL","postal_code":"60607","country":"US"}}}'
```

## 4. The payment handlers

`POST /checkout_sessions/{id}/complete` takes `payment_data.handler_id`:

- `card`: a raw card number (the baseline; no agent-payment protocol).
- `ap2`: an AP2 mandate chain plus a credential-provider token. Human-not-present.
- `direct`: a user-signed approval from a trusted surface. Human-present.

For `ap2` and `direct` the agent first calls `POST /checkout_sessions/{id}/ap2` to get the
merchant-signed checkout and a single-use nonce. The merchant publishes the key that signs checkouts
at `/.well-known/ap2-merchant-key`.

## 5. The real payment rail (Stripe test mode)

```bash
# store a Stripe TEST secret key (sk_test_...) in the macOS Keychain, never in the repo:
security add-generic-password -U -a "$USER" -s stripe-test-key -w   # paste the key at the prompt
export STRIPE_TEST_KEY="$(security find-generic-password -a "$USER" -s stripe-test-key -w)"
pip install -e ".[stripe]"
shipjack-serve --stripe
```

Each completed order creates and confirms a Stripe PaymentIntent in test mode. The adapter refuses
any key that is not a test key, so it can never move real money. All AP2 and card checks run before
Stripe is called, and a Stripe decline leaves the order unpaid and the token unspent.

## 6. The MCP transport

```bash
shipjack-mcp            # JSON-RPC 2.0 over stdio: initialize, tools/list, tools/call
```

The five ACP operations are MCP tools (`create/get/update/complete/cancel_checkout_session`), called
with `{meta, id?, payload?}`, plus `search_products`, `get_product` and `get_ap2_checkout`.

## 7. After the order: refunds, evidence and the audit trail

```bash
curl -s http://127.0.0.1:8000/orders/ord_cs_northwind_1 -H "Authorization: Bearer demo-token" -H "API-Version: 2026-04-17"
curl -s -X POST http://127.0.0.1:8000/orders/ord_cs_northwind_1/refund \
  -H "Authorization: Bearer demo-token" -H "API-Version: 2026-04-17" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" -d '{"amount": 1000}'          # omit amount for a full refund
curl -s http://127.0.0.1:8000/orders/ord_cs_northwind_1/evidence -H "Authorization: Bearer demo-token" -H "API-Version: 2026-04-17"
```

- **Refunds** go back on the rail that charged: a real Stripe Refund in test mode, or the simulated
  processor. A merchant cannot refund more than it took, and two refunds at once return the money once.
  A failed Stripe refund changes nothing.
- **Dispute evidence** rebuilds an order from stored records, the way AP2's "Dispute Evidence" section
  describes: the merchant-signed checkout and its hash, the mandate (or user approval) reference, the
  signed receipt, the charge with its PaymentIntent id, and every refund.
- **The audit trail** (`Merchant.audit`) is an append-only log: session created, checkout signed, order
  paid, order refunded. It holds ids, amounts and rail references, not card numbers or keys.

## 8. The delivery constraint (recommended default on)

AP2 v0.2 does not constrain where goods are delivered. shipjack adds a checkout constraint,
`io.github.srivatsa03.fulfillment`, that pins the allowed ship-to address and delivery email. The
wallet checks it before signing and the merchant checks it again. Keep it on; see
[AP2 issue #381](https://github.com/google-agentic-commerce/AP2/issues/381) and the README.

## 9. Ship gate: the safety suite

Run `python -m pytest -q` before any change goes out. The suite is the deployment gate: AP2 MUSTs,
the ACP API on bad input, address and email matching, concurrency, and seeded fuzzing. `shipjack
bench` prints what each payment profile stops.

## What is real and what is simulated

- **Real:** the ACP REST contract and MCP binding, the AP2 SDK cryptography, the Stripe test-mode
  settlement, the idempotency and concurrency handling.
- **Simulated:** the product catalog, the merchant's onboarding, order fulfilment, and (without a
  Stripe key) the money. Swapping the catalog and the processor for a merchant's real systems is the
  remaining integration work.
