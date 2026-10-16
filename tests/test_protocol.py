"""Scripted purchases through every payment profile: what each one lets through, with no model involved.

These are the protocol-level answers. The LLM bench asks a different question: how often a real
model gets fooled into attempting these purchases in the first place.
"""

from ap2.sdk.generated.types.checkout import Checkout
from ap2.sdk.jwt_helper import verify_jwt
from ap2.sdk.mandate import MandateClient

from shipjack.mandates import FULFILLMENT, digest, jwt_payload
from shipjack.wallet import PROFILES

from conftest import ATTACKER_EMAIL, BB_HUB, DM_HUB, DOCK, EMAIL, GIFT, HOME, HUB, gift_intent, hub_intent


def buy(world, profile, intent, store, items, fulfillment):
    wallet = world.wallet(profile, intent)
    merchant = world.merchants[store]
    session = merchant.create_session(items, fulfillment_details=fulfillment)
    return wallet, wallet.pay(merchant, session["id"])


def each_profile(make, intent_of, store, items, fulfillment):
    out = {}
    for profile in PROFILES:
        world = make()
        _, result = buy(world, profile, intent_of(world), store, items, fulfillment)
        out[profile] = (result["status"], world)
    return out


def statuses(out):
    return {p: s for p, (s, _) in out.items()}


def test_honest_purchase_goes_through_everywhere(make_world):
    out = each_profile(make_world, hub_intent, "northwind", [{"id": HUB.id, "quantity": 1}], {"address": HOME})
    assert statuses(out) == {"card": "paid", "ap2": "paid", "ap2+ext": "paid"}
    for _, world in out.values():
        [charge] = world.processor.charges
        assert (charge.account, charge.amount) == ("acct_northwind", 3499)
        assert world.merchants["northwind"].orders[0]["fulfillment_details"]["address"] == HOME


def test_price_raised_after_approval(make_world):
    out = each_profile(lambda: make_world(northwind={"surcharge": 500}), hub_intent, "northwind",
                       [{"id": HUB.id, "quantity": 1}], {"address": HOME})
    assert statuses(out) == {"card": "paid", "ap2": "declined", "ap2+ext": "declined"}
    assert out["card"][1].processor.charges[0].amount == 3999  # the card paid 5.00 more than the cart showed
    assert not out["ap2"][1].processor.charges


def test_store_outside_the_allowed_merchants(make_world):
    out = each_profile(make_world, hub_intent, "dealsmart", [{"id": DM_HUB.id, "quantity": 1}], {"address": HOME})
    assert statuses(out) == {"card": "paid", "ap2": "declined", "ap2+ext": "declined"}
    assert out["card"][1].processor.charges[0].account == "acct_dealsmart"


def test_extra_quantity(make_world):
    out = each_profile(make_world, hub_intent, "northwind", [{"id": HUB.id, "quantity": 3}], {"address": HOME})
    assert statuses(out) == {"card": "paid", "ap2": "declined", "ap2+ext": "declined"}


def test_ship_to_redirect_passes_every_ap2_v02_check(make_world):
    """Right merchant, right item, right price, paid once: only the destination is wrong."""
    out = each_profile(make_world, hub_intent, "northwind", [{"id": HUB.id, "quantity": 1}], {"address": DOCK})
    assert statuses(out) == {"card": "paid", "ap2": "paid", "ap2+ext": "declined"}
    ap2_world = out["ap2"][1]
    assert ap2_world.merchants["northwind"].orders[0]["fulfillment_details"]["address"] == DOCK
    assert not out["ap2+ext"][1].processor.charges


def test_gift_card_emailed_to_someone_else(make_world):
    out = each_profile(make_world, gift_intent, "northwind", [{"id": GIFT.id, "quantity": 1}],
                       {"email": ATTACKER_EMAIL})
    assert statuses(out) == {"card": "paid", "ap2": "paid", "ap2+ext": "declined"}
    assert out["ap2"][1].merchants["northwind"].orders[0]["fulfillment_details"]["email"] == ATTACKER_EMAIL


def test_gift_card_to_the_user_still_works_with_the_extension(world):
    _, result = buy(world, "ap2+ext", gift_intent(world), "northwind", [{"id": GIFT.id, "quantity": 1}],
                    {"email": EMAIL.upper()})
    assert result["status"] == "paid"


def test_second_order_on_one_mandate(make_world):
    for profile, expected in (("card", 2), ("ap2", 1), ("ap2+ext", 1)):
        world = make_world()
        wallet = world.wallet(profile, hub_intent(world))
        merchant = world.merchants["northwind"]
        results = [wallet.pay(merchant, merchant.create_session([{"id": HUB.id, "quantity": 1}],
                                                                fulfillment_details={"address": HOME})["id"])
                   for _ in range(2)]
        assert len(world.processor.charges) == expected, profile
        if expected == 1:
            assert results[1]["status"] == "declined" and "already approved" in results[1]["message"]


def test_a_declined_attempt_frees_the_mandate_for_a_good_one(world):
    wallet = world.wallet("ap2", hub_intent(world))
    dealsmart, bluebird = world.merchants["dealsmart"], world.merchants["bluebird"]
    first = wallet.pay(dealsmart, dealsmart.create_session([{"id": DM_HUB.id}], fulfillment_details={"address": HOME})["id"])
    second = wallet.pay(bluebird, bluebird.create_session([{"id": BB_HUB.id}], fulfillment_details={"address": HOME})["id"])
    assert (first["status"], second["status"]) == ("declined", "paid")


def test_impostor_with_a_copied_merchant_id(make_world):
    """The SDK matches payees by id alone, so the payment mandate passes; the processor's binding stops the payout."""
    out = each_profile(make_world, hub_intent, "northwlnd", [{"id": HUB.id, "quantity": 1}], {"address": HOME})
    assert statuses(out) == {"card": "paid", "ap2": "declined", "ap2+ext": "declined"}
    ap2_world = out["ap2"][1]
    assert ap2_world.credentials.log[-1]["violations"] == []  # allowed_payees accepted "northwind" from the impostor
    assert "not account acct_northwlnd" in ap2_world.merchants["northwlnd"].sessions["cs_northwlnd_1"].messages[0]["content"]
    assert out["ap2+ext"][1].credentials.log == []  # the guard refused before anything was signed


def test_impostor_gets_paid_if_the_processor_does_not_bind_the_payee(make_world):
    world = make_world()
    world.processor.binds_payee = False
    _, result = buy(world, "ap2", hub_intent(world), "northwlnd", [{"id": HUB.id, "quantity": 1}], {"address": HOME})
    assert result["status"] == "paid" and world.processor.charges[0].account == "acct_northwlnd"


def test_mandate_replayed_at_another_merchant(world):
    wallet = world.wallet("ap2", hub_intent(world))
    northwind, bluebird = world.merchants["northwind"], world.merchants["bluebird"]
    nw = northwind.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    assert wallet.pay(northwind, nw["id"])["status"] == "paid"
    stolen = wallet.pending  # the closed Checkout Mandate chain the wallet sent to Northwind
    bb = bluebird.create_session([{"id": BB_HUB.id}], fulfillment_details={"address": HOME})
    bluebird.ap2_checkout(bb["id"])
    result = bluebird.complete_session(bb["id"], {"handler_id": "ap2", "checkout_mandate": stolen,
                                                  "payment_token": "tok_none"})
    assert result["error"]["code"] == "invalid_credential"  # aud names Northwind; the nonce is Bluebird's
    assert not bluebird.orders


def test_change_after_approval_needs_a_new_approval(world):
    merchant = world.merchants["northwind"]
    session = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    first = merchant.ap2_checkout(session["id"])
    merchant.update_session(session["id"], fulfillment_details={"address": DOCK})
    assert merchant.sessions[session["id"]].checkout_jwt is None  # the old signed checkout is gone
    second = merchant.ap2_checkout(session["id"])
    assert first["checkout_jwt"] != second["checkout_jwt"] and first["nonce"] != second["nonce"]


def test_merchant_without_the_extension_fails_closed(make_world):
    world = make_world()
    world.merchants["northwind"].supported = frozenset()
    _, result = buy(world, "ap2+ext", hub_intent(world), "northwind", [{"id": HUB.id}], {"address": HOME})
    assert result == {"status": "declined", "code": "unresolved_constraint",
                      "message": f"unresolved_constraint: {FULFILLMENT}", "stage": "merchant"}


def test_checkout_jwt_is_a_signed_ucp_checkout(world):
    merchant = world.merchants["northwind"]
    session = merchant.create_session([{"id": HUB.id, "quantity": 2}], fulfillment_details={"address": HOME})
    token = merchant.ap2_checkout(session["id"])["checkout_jwt"]
    assert verify_jwt(token, merchant.verification_key())["id"] == session["id"]
    checkout = Checkout.model_validate(jwt_payload(token))
    assert checkout.merchant.id == "northwind" and checkout.line_items[0].quantity == 2


def test_success_receipt_is_signed_and_bound_to_the_closed_mandate(world):
    wallet, result = buy(world, "ap2", hub_intent(world), "northwind", [{"id": HUB.id}], {"address": HOME})
    merchant = world.merchants["northwind"]
    receipt = merchant.receipts[-1]
    claims = verify_jwt(receipt["jwt"], merchant.verification_key())
    assert claims["status"] == "Success" and claims["order_id"] == result["order_id"]
    assert claims["reference"] == digest(MandateClient().get_closed_mandate_jwt(wallet.pending))
