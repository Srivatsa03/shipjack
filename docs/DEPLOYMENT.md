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

## 7. After the order: refunds, fulfillment, evidence and the audit trail

The agent platform reads an order; only the merchant moves money or goods. Those routes take an
*operator* token (the demo store's is `demo-operator`, or set `SHIPJACK_OPERATOR_TOKEN`), and an
agent platform's token gets `403 forbidden` there.

```bash
V="API-Version: 2026-04-17"; J="Content-Type: application/json"
PLATFORM="Authorization: Bearer demo-token"; OPERATOR="Authorization: Bearer demo-operator"
curl -s http://127.0.0.1:8000/orders/ord_cs_northwind_1 -H "$PLATFORM" -H "$V"      # the ACP Order
curl -s -X POST http://127.0.0.1:8000/orders/ord_cs_northwind_1/refund -H "$OPERATOR" -H "$V" -H "$J" \
  -H "Idempotency-Key: $(uuidgen)" -d '{"amount": 1000}'          # omit amount for a full refund
curl -s -X POST http://127.0.0.1:8000/orders/ord_cs_northwind_1/fulfill -H "$OPERATOR" -H "$V" -H "$J" \
  -H "Idempotency-Key: $(uuidgen)" -d '{"carrier": "USPS", "tracking_number": "9400111899223456789012"}'
curl -s -X POST http://127.0.0.1:8000/orders/ord_cs_northwind_1/deliver -H "$OPERATOR" -H "$V" -H "$J" \
  -H "Idempotency-Key: $(uuidgen)" -d '{}'
curl -s http://127.0.0.1:8000/orders/ord_cs_northwind_1/evidence -H "$OPERATOR" -H "$V"
```

- **The order** is the ACP 2026-04-17 Order object: line items with `ordered`/`current`/`fulfilled`
  quantities and a derived status, fulfillments with tracking and events, adjustments for refunds,
  and totals including `amount_refunded`. Status runs `created` → `shipped` → `completed`, or
  `created` → `canceled` (cancel refunds whatever is left). A digital-only order is `completed` once
  delivered. Steps out of order (deliver before shipping, cancel after shipping) are refused.

- **Refunds** go back on the rail that charged: a real Stripe Refund in test mode, or the simulated
  processor. A merchant cannot refund more than it took, and two refunds at once return the money once.
  A failed Stripe refund changes nothing.
- **Dispute evidence** rebuilds an order from stored records, the way AP2's "Dispute Evidence" section
  describes: the merchant-signed checkout and its hash, the mandate (or user approval) reference, the
  signed receipt, the charge with its PaymentIntent id, and every refund.
- **The audit trail** (`Merchant.audit`) is an append-only log: session created, checkout signed, order
  paid, refunded, fulfilled, delivered, canceled. It holds ids, amounts and rail references, not card
  numbers or keys.

## 8. Order webhooks: telling the agent platform what happened

An agent needs to answer "where's my order?". ACP's answer is order webhooks: the merchant POSTs an
event to the platform's receiver on every change. shipjack sends them as the 2026-04-17 spec says
(full Order snapshot, never a delta; `Merchant-Signature: t=<unix>,v1=<HMAC-SHA256 of t.body>`), and
adds what an integration needs to run unattended.

```bash
# the secret you share with the platform: from the environment, never on a command line
export SHIPJACK_WEBHOOK_SECRET="$(openssl rand -hex 32)"
shipjack-receiver --port 9000 &        # a reference receiver that verifies and prints each event
shipjack-serve --webhook-url http://127.0.0.1:9000/agentic_checkout/webhooks/order_events
```

| Situation | What happens |
|---|---|
| Receiver answers 2xx | Delivered. |
| Network error, 408, 425, 429 or 5xx | Retried with exponential backoff (1 s, 2 s, 4 s ... capped at 5 min, with jitter), same `Request-Id` every time, up to 8 attempts, then dead-lettered. |
| Any other 4xx (or 3xx) | The event itself is wrong (bad secret, bad payload): dead-lettered at once, not retried forever. |
| An order's event is waiting to retry | That order's later events wait behind it, so "created" can never arrive after "shipped". Other orders are not held. |
| URL is plain `http://` | Refused unless it is localhost: signed order data leaves only over HTTPS. |

The reference receiver (`shipjack.webhooks.create_receiver`) is what the platform side should do:
401 `invalid_signature` for a missing, malformed, stale (over 300 s) or wrong signature, then three
replay defenses. `Request-Id` is not covered by the signature, so it also remembers the signatures it
accepted; a retry carries the same `Request-Id`; and an older snapshot of an order (earlier `t`) never
replaces a newer one, so a captured "created" replayed after "shipped" cannot roll the order back.
Unknown event types and statuses are accepted, as the spec requires.

Today the outbox lives in memory: events queued when the process stops are lost. A merchant running
this for real puts the outbox in the same database transaction as the order change.

## 9. The delivery constraint (recommended default on)

AP2 v0.2 does not constrain where goods are delivered. shipjack adds a checkout constraint,
`io.github.srivatsa03.fulfillment`, that pins the allowed ship-to address and delivery email. The
wallet checks it before signing and the merchant checks it again. Keep it on; see
[AP2 issue #381](https://github.com/google-agentic-commerce/AP2/issues/381) and the README.

## 10. Ship gate: the safety suite

Run `python -m pytest -q` before any change goes out. The suite is the deployment gate: AP2 MUSTs,
the ACP API on bad input, address and email matching, concurrency, and seeded fuzzing. `shipjack
bench` prints what each payment profile stops.

## What is real and what is simulated

- **Real:** the ACP REST contract and MCP binding, the AP2 SDK cryptography, the Stripe test-mode
  settlement, signed order webhooks over HTTP, the idempotency and concurrency handling.
- **Simulated:** the product catalog, the merchant's onboarding, the carrier (an operator marks an
  order shipped and delivered), and (without a Stripe key) the money. Swapping the catalog and the processor for a merchant's real systems is the
  remaining integration work.
