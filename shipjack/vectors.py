"""AP2 authority vectors for the fulfillment destination (where the goods go), in the two-record format
of the shared set from AP2 Discussion #332 (C0 to C4b).

Each vector keeps two records apart:

- ``sdk``: what the AP2 Python SDK at the pinned commit actually evaluates, run for real on a signed
  open mandate, a merchant-signed checkout and the agent's closed mandate.
- ``authority``: what the specification establishes about whether the user delegated that choice.
  Passing SDK checks are never recorded as proof that the destination was authorized.

The destination can change at two points, and they are different questions:

- before the closed mandate is signed (F1 to F4): the agent picks the destination and the closed
  mandate then binds that checkout correctly. Integrity holds; the question is authority (C4b).
- after the closed mandate is signed (F5): the bound checkout carries the destination, so changing it
  is a closed-to-execution integrity failure (C1), tracked for the SDK in AP2 #358 / #359.

``io.github.srivatsa03.fulfillment`` (F3, F4) is an experimental constraint type proposed in AP2 #381,
not part of AP2. Signatures change on every run (fresh keys); the recorded outcomes do not.

    shipjack ap2-vectors --json docs/ap2_fulfillment_vectors.json
"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError, distribution

from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.types.checkout import Checkout

from .keys import new_key
from .mandates import (
    FULFILLMENT,
    TrustList,
    client,
    digest,
    evaluate_checkout,
    jwt_payload,
    present,
)
from .scenarios import (
    DOCK,
    DROP_EMAIL,
    EMAIL,
    GIFT,
    HOME,
    HUB,
    gift_intent,
    hub_intent,
    stores,
)
from .world import build

SPEC = "docs/ap2/agent_authorization.md"
_HASH = re.compile(r"\b[A-Za-z0-9_-]{43}\b")  # an unpadded base64url SHA-256
C4B_BASIS = (f"{SPEC} makes constraints OPTIONAL and evaluates the constraints present in the open mandate; "
             "the v0.2 checkout vocabulary (checkout.allowed_merchants, checkout.line_items) has no type for "
             "the destination, and no text says whether the agent's choice of destination was delegated.")


def sdk_commit() -> str:
    """The AP2 commit the installed SDK was built from (pip records it for a git install)."""
    try:
        direct = json.loads(distribution("ap2").read_text("direct_url.json") or "{}")
        return (direct.get("vcs_info") or {}).get("commit_id", "unknown")[:7]
    except PackageNotFoundError:
        return "unknown"


class _Purchase:
    """One delegation and one store, with every role played by hand."""

    def __init__(self, intent_of, *, extension: bool):
        self.world = build(stores())
        self.merchant = self.world.merchants["northwind"]
        self.agent_key = new_key("agent-under-test")
        self.intent = intent_of(self.world)
        self.deleg = self.world.surface.delegate(self.intent, self.agent_key, extensions=extension)
        self.trust = TrustList([self.world.surface.verification_key()])

    def checkout(self, product, fulfillment: dict) -> dict:
        """The merchant signs a UCP checkout naming this destination, with a nonce for its approval."""
        session = self.merchant.create_session([{"id": product.id, "quantity": 1}], fulfillment_details=fulfillment)
        return self.merchant.ap2_checkout(session["id"])

    def close(self, signed: dict) -> str:
        """The agent closes the open checkout mandate over that exact checkout."""
        jwt = signed["checkout_jwt"]
        return present(self.agent_key, self.deleg.open_checkout,
                       CheckoutMandate(checkout_jwt=jwt, checkout_hash=digest(jwt)),
                       aud=signed["aud"], nonce=signed["nonce"])


def _sdk_record(p: _Purchase, chain_token: str, signed: dict, presented_jwt: str,
                expected_hash: str | None) -> dict:
    """Run the unmodified SDK the way a verifier calls it, and note what it looked at."""
    rec: dict = {}
    try:
        payloads = client.verify(token=chain_token, key_or_provider=p.trust,
                                 expected_aud=signed["aud"], expected_nonce=signed["nonce"])
        rec["chain_signatures_aud_nonce"] = "verified"
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        rec["chain_signatures_aud_nonce"] = f"rejected: {type(exc).__name__}"
        return rec
    try:
        chain = CheckoutMandateChain.parse(payloads)
        rec["chain_parse"] = "ok"
    except Exception as exc:  # noqa: BLE001 - an unknown constraint type is not in the SDK's model
        types = [c.get("type") for c in payloads[0].get("constraints", [])]
        unknown = [t for t in types if t not in ("checkout.allowed_merchants", "checkout.line_items")]
        rec["chain_parse"] = f"rejected: {type(exc).__name__} (constraint types not in the SDK model: {unknown})"
        rec["result"] = "fails closed (C0: an unknown constraint fails evaluation)"
        return rec
    rec["open_mandate_constraints"] = [c.type for c in chain.open_mandate.constraints]
    rec["destination_in_signed_checkout"] = "fulfillment_details" in jwt_payload(presented_jwt)
    rec["destination_is_a_checkout_model_field"] = "fulfillment_details" in Checkout.model_fields
    rec["expected_checkout_hash_passed"] = expected_hash is not None
    rec["violations"] = [_HASH.sub("<sha256>", v)  # hashes differ every run (fresh keys); the outcome doesn't
                         for v in chain.verify(expected_checkout_hash=expected_hash, checkout_jwt=presented_jwt)]
    rec["result"] = "no violations" if not rec["violations"] else "violations"
    return rec


def _redirect(intent_of, product, fulfillment: dict, destination: str) -> dict:
    p = _Purchase(intent_of, extension=False)
    signed = p.checkout(product, fulfillment)
    token = p.close(signed)
    jwt = signed["checkout_jwt"]
    return {"sdk": _sdk_record(p, token, signed, jwt, expected_hash=digest(jwt)),
            "checkout_destination": destination}


def _constrained(intent_of, product, honest: dict, attack: dict) -> dict:
    """The same purchase with the experimental fulfillment constraint in the open mandate."""
    p = _Purchase(intent_of, extension=True)
    out = {}
    for label, fulfillment in (("attacker_destination", attack), ("users_own_destination", honest)):
        signed = p.checkout(product, fulfillment)
        token = p.close(signed)
        jwt = signed["checkout_jwt"]
        open_raw = client.verify(token=token, key_or_provider=p.trust, expected_aud=signed["aud"],
                                 expected_nonce=signed["nonce"])[0]
        evaluated = evaluate_checkout(open_raw, jwt, supported={FULFILLMENT})
        out[label] = {
            "sdk_unmodified": _sdk_record(p, token, signed, jwt, expected_hash=digest(jwt)),
            "sdk_plus_fulfillment_evaluator": {"violations": evaluated,
                                               "result": "violations" if evaluated else "no violations"},
        }
    return out


def _after_signing() -> dict:
    """The closed mandate binds checkout A (the user's address); the checkout presented at execution is B."""
    p = _Purchase(hub_intent, extension=False)
    bound = p.checkout(HUB, {"address": HOME, "email": EMAIL})
    token = p.close(bound)
    changed = p.checkout(HUB, {"address": DOCK, "email": EMAIL})  # same cart, re-signed after the address changed
    presented = changed["checkout_jwt"]
    return {
        "sdk_caller_passes_no_expected_hash": _sdk_record(p, token, bound, presented, expected_hash=None),
        "sdk_hash_derived_from_presented_checkout": _sdk_record(p, token, bound, presented,
                                                                expected_hash=digest(presented)),
    }


def build_vectors() -> dict:
    home = f"{HOME['line_one']}, {HOME['city']}"
    dock = f"{DOCK['line_one']}, {DOCK['city']}"
    vectors = [
        {"id": "F1", "case": "C4b", "stage": "before the closed mandate is signed (open -> closed)",
         "title": "Physical goods ship to an address the user never gave",
         "open_mandate": "checkout.allowed_merchants = [northwind], checkout.line_items = [nw-hub-7 x1]",
         "users_destination": home,
         **_redirect(hub_intent, HUB, {"address": DOCK, "email": EMAIL}, dock),
         "authority": {"status": "NORMATIVE_QUESTION", "basis": C4B_BASIS}},
        {"id": "F2", "case": "C4b", "stage": "before the closed mandate is signed (open -> closed)",
         "title": "A digital good (gift card code) is emailed to an inbox the user never gave",
         "open_mandate": "checkout.allowed_merchants = [northwind], checkout.line_items = [nw-gift-25 x1]",
         "users_destination": EMAIL,
         **_redirect(gift_intent, GIFT, {"email": DROP_EMAIL}, DROP_EMAIL),
         "authority": {"status": "NORMATIVE_QUESTION", "basis": C4B_BASIS}},
        {"id": "F3", "case": "F1 + experimental constraint",
         "stage": "before the closed mandate is signed (open -> closed)",
         "title": "F1 with io.github.srivatsa03.fulfillment {ship_to: [the user's address]} in the open mandate",
         "open_mandate": "F1's constraints + io.github.srivatsa03.fulfillment",
         "users_destination": home, "checkout_destination": dock,
         **_constrained(hub_intent, HUB, {"address": HOME, "email": EMAIL}, {"address": DOCK, "email": EMAIL}),
         "authority": {"status": "C3 where the type is implemented; C0 where it is not",
                       "basis": "With the constraint present the destination is a declared dimension, so a "
                                "false value is an authority violation (C3) for a verifier that implements "
                                "the type. A verifier that does not know it MUST fail it (" + SPEC + ", "
                                "Verification and Processing Rules, step 3). The type is experimental (AP2 #381), "
                                "not part of AP2."}},
        {"id": "F4", "case": "F2 + experimental constraint",
         "stage": "before the closed mandate is signed (open -> closed)",
         "title": "F2 with io.github.srivatsa03.fulfillment {deliver_to: [the user's email]} in the open mandate",
         "open_mandate": "F2's constraints + io.github.srivatsa03.fulfillment",
         "users_destination": EMAIL, "checkout_destination": DROP_EMAIL,
         **_constrained(gift_intent, GIFT, {"email": EMAIL}, {"email": DROP_EMAIL}),
         "authority": {"status": "C3 where the type is implemented; C0 where it is not",
                       "basis": "As F3."}},
        {"id": "F5", "case": "C1", "stage": "after the closed mandate is signed (closed -> execution)",
         "title": "The address changes after the closed mandate bound the checkout",
         "open_mandate": "as F1",
         "users_destination": home, "bound_checkout_destination": home, "presented_checkout_destination": dock,
         **_after_signing(),
         "authority": {"status": "established for this draft: integrity failure, MUST reject (C1)",
                       "basis": "The destination is part of the merchant-signed checkout the closed mandate "
                                "hashes, so a changed destination is a different checkout. In the SDK at this "
                                "commit the hash is compared only when the caller passes expected_checkout_hash "
                                "(AP2 #358; #359 derives it from the presented checkout_jwt). Not a C4b question."}},
    ]
    return {"format": "two records per vector: sdk = what the SDK evaluates; authority = what the spec establishes",
            "ap2_sdk_commit": sdk_commit(), "spec": SPEC, "shared_set": "AP2 Discussion #332 (C0-C4b)",
            "vectors": vectors}


def table(report: dict) -> str:
    lines = [f"AP2 SDK {report['ap2_sdk_commit']}", "",
             "| Vector | Stage | SDK result | Authority |", "|---|---|---|---|"]
    for v in report["vectors"]:
        if "sdk" in v:
            sdk = v["sdk"]["result"]
        elif "attacker_destination" in v:
            a = v["attacker_destination"]
            sdk = (f"unmodified: {a['sdk_unmodified']['result']}; with the evaluator: "
                   f"{a['sdk_plus_fulfillment_evaluator']['result']} "
                   f"(own destination: {v['users_own_destination']['sdk_plus_fulfillment_evaluator']['result']})")
        else:
            sdk = (f"no expected hash: {v['sdk_caller_passes_no_expected_hash']['result']}; hash of the presented "
                   f"checkout: {v['sdk_hash_derived_from_presented_checkout']['result']}")
        lines.append(f"| {v['id']} {v['title']} | {v['stage']} | {sdk} | {v['authority']['status']} |")
    return "\n".join(lines)


def add_parser(sub) -> None:
    p = sub.add_parser("ap2-vectors", help="AP2 authority vectors for the fulfillment destination (two-record format)")
    p.add_argument("--json", help="also write the report here")
    p.set_defaults(func=main)


def main(args) -> int:
    report = build_vectors()
    print(table(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
            f.write("\n")
    return 0
