# AP2 authority vectors: fulfillment destination

> Status: draft for review in AP2 Discussion
> [#332](https://github.com/google-agentic-commerce/AP2/discussions/332), following the shared set
> (C0, C1 to C3, C4a, C4b) started there. Proposal: AP2 issue
> [#381](https://github.com/google-agentic-commerce/AP2/issues/381).
>
> Each vector has two records, kept apart on purpose:
> - **sdk**: what the AP2 Python SDK at `e1ea56d` actually evaluates. Every vector is run for real: a
>   user-signed open mandate, a merchant-signed UCP checkout, and the agent's key-binding hop over it.
> - **authority**: what the specification establishes about whether the user delegated the choice.
>   Passing SDK checks are not recorded as proof that a destination was authorized.
>
> Run it: `shipjack ap2-vectors --json out.json` (the full records are in
> [ap2_fulfillment_vectors.json](ap2_fulfillment_vectors.json); `tests/test_ap2_vectors.py` keeps them true).

The destination can change at two points, and they are different questions:

- **Before the closed mandate is signed** (F1 to F4). The agent picks the destination, then binds that
  checkout correctly. Integrity holds. The question is whether the open mandate gave the agent
  authority to choose it: an authority-bearing dimension the v0.2 vocabulary cannot express (C4b).
- **After the closed mandate is signed** (F5). The destination is in the merchant-signed checkout the
  closed mandate hashes, so changing it makes a different checkout: closed to execution integrity (C1),
  tracked for the SDK in AP2 #358 / #359.

Common setup: the user wants one Northwind 7-in-1 USB-C Hub shipped to their home (F1, F3, F5) or one
Northwind $25 gift card emailed to their own inbox (F2, F4). The open checkout mandate has
`checkout.allowed_merchants = [northwind]` and `checkout.line_items = [that product x1]`. The checkout
carries the destination in `fulfillment_details` (ACP's field), inside the signed checkout JWT.

## F1 (C4b): physical goods ship to an address the user never gave

- **Derived checkout:** right merchant, right item, right price; ship-to `77 Dock St, Gary`.
- **sdk:** chain signatures, `aud` and `nonce` verified; `CheckoutMandateChain.verify` with
  `expected_checkout_hash` = hash of the presented checkout: **no violations**. The destination is in
  the signed checkout but is not a field of the SDK's `Checkout` model; it is carried as an extra
  property and never evaluated.
- **authority:** `NORMATIVE_QUESTION`. `constraints` are OPTIONAL and verification evaluates the
  constraints present; no v0.2 checkout constraint type expresses a destination, and no text says
  whether the agent's choice of destination was delegated.

## F2 (C4b): a gift card code is emailed to an inbox the user never gave

- **Derived checkout:** right merchant, item and price; deliver-to `codes@northwind-fulfillment.example`.
- **sdk:** as F1, **no violations**.
- **authority:** `NORMATIVE_QUESTION`, as F1.

## F3: F1 with an explicit fulfillment constraint

The open mandate adds `io.github.srivatsa03.fulfillment {ship_to: [the user's address]}`, an
experimental type proposed in #381 (not part of AP2).

| Checkout | Unmodified SDK | SDK + the constraint's evaluator |
|---|---|---|
| ship-to `77 Dock St, Gary` | rejected when the chain is parsed: the type is not in the SDK's constraint model | violation: `ship-to 77 Dock St, Gary is not allowed` |
| ship-to the user's own address | rejected the same way | no violations |

- **authority:** with the constraint present, the destination is a declared dimension: a false value
  is an authority violation (C3) for a verifier that implements the type. A verifier that does not know
  the type MUST fail it (`agent_authorization.md`, Verification and Processing Rules, step 3): C0. That
  is safe but not interoperable, since it also stops the user's own purchase, which is why a shared
  definition is needed.

## F4: F2 with an explicit fulfillment constraint

Same as F3 with `deliver_to: [the user's email]`: the attacker's inbox is a violation, the user's own
inbox passes, and the unmodified SDK rejects both at parse time.

## F5 (C1): the address changes after the closed mandate is signed

- **Bound checkout:** ship-to the user's home. **Presented at execution:** the same cart, re-signed by
  the merchant with ship-to `77 Dock St, Gary`.
- **sdk:** `CheckoutMandateChain.verify(checkout_jwt=presented)` with no `expected_checkout_hash`:
  **no violations** (the closed mandate's hash is compared only when the caller passes one; #358). With
  the hash of the presented checkout passed in (what #359 derives itself): `checkout_hash mismatch`.
- **authority:** established for this draft: integrity failure, MUST reject (C1). Not a C4b question.

## Summary

| Vector | Stage | SDK result | Authority |
|---|---|---|---|
| F1 ship-to redirect | before signing | no violations | `NORMATIVE_QUESTION` (C4b) |
| F2 deliver-to redirect | before signing | no violations | `NORMATIVE_QUESTION` (C4b) |
| F3 F1 + fulfillment constraint | before signing | unmodified: fails closed; with evaluator: violation (own address passes) | C3 where implemented, C0 where not |
| F4 F2 + fulfillment constraint | before signing | unmodified: fails closed; with evaluator: violation (own inbox passes) | C3 where implemented, C0 where not |
| F5 address changed after signing | closed to execution | no expected hash: no violations; hash of presented checkout: mismatch | C1, MUST reject |
