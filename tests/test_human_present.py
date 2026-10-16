"""Human-present (Direct) mode: the user signs each purchase after reading an approval screen.

The headline: whether a careful, present user catches a redirected order depends on what the screen
shows. AP2's example screen (``Screen.SPEC``) shows the merchant, amount and items but not the
destination, so the user signs a redirect they cannot see. ``Screen.WITH_DESTINATION`` shows it, and
the same user declines. This is the human-present face of the delivery gap in AP2 issue #381.
"""


import pytest

from conftest import EMAIL, GIFT, HOME, HUB, gift_intent, hub_intent
from shipjack.direct import (DirectSurface, Screen, new_user, purchase_direct, render,
                             request_from_checkout, verify_user_approval)

ATTACKER = {"name": "Mallory", "line_one": "77 Dock St", "line_two": "", "city": "Gary", "state": "IN",
            "postal_code": "46402", "country": "US"}


def buy(world, store, items, fulfillment, intent_of, screen, user=None):
    merchant = world.merchants[store]
    session = merchant.create_session([{"id": p, "quantity": q} for p, q in items], fulfillment_details=fulfillment)
    user = user or new_user(intent_of(world))
    return purchase_direct(world, store, session["id"], user, screen), user


# ---- the honest purchase goes through on either screen -------------------------------------------


@pytest.mark.parametrize("screen", list(Screen))
def test_an_honest_purchase_is_signed_and_paid(world, screen):
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": HOME}, hub_intent, screen)
    assert result["status"] == "paid" and result["amount"] == 3499
    assert world.processor.charges[0].account == "acct_northwind"


# ---- the finding: the screen decides whether the user can catch a redirect -----------------------


def test_the_spec_screen_hides_a_ship_to_redirect_so_the_user_signs_it(world):
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": ATTACKER}, hub_intent, Screen.SPEC)
    assert result["status"] == "paid"  # the user never saw the address, so they approved it
    assert world.merchants["northwind"].orders[0]["fulfillment_details"]["address"]["line_one"] == "77 Dock St"


def test_the_destination_screen_lets_the_same_user_catch_the_ship_to_redirect(world):
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": ATTACKER}, hub_intent, Screen.WITH_DESTINATION)
    assert result["status"] == "declined" and result["stage"] == "user"
    assert "ships to 77 Dock St" in result["message"] and not world.processor.charges


def test_the_spec_screen_hides_a_gift_card_email_redirect(world):
    result, _ = buy(world, "northwind", [(GIFT.id, 1)], {"email": "codes@evil.test"}, gift_intent, Screen.SPEC)
    assert result["status"] == "paid"


def test_the_destination_screen_catches_the_gift_card_email_redirect(world):
    result, _ = buy(world, "northwind", [(GIFT.id, 1)], {"email": "codes@evil.test"}, gift_intent,
                    Screen.WITH_DESTINATION)
    assert result["status"] == "declined" and "emails codes@evil.test" in result["message"]


# ---- things both screens show, so the user catches them either way -------------------------------


@pytest.mark.parametrize("screen", list(Screen))
def test_a_displayed_price_over_budget_is_declined_by_the_user(world, screen):
    from shipjack.surface import Intent
    tight = lambda w: Intent(task="buy a hub under $30", max_total=3000, merchants=[w.info("northwind")],
                             items=[([(HUB.id, HUB.title)], 1)], ship_to=HOME, email=EMAIL)
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": HOME}, tight, screen)  # $34.99 shown, $30 budget
    assert result["status"] == "declined" and "over my budget" in result["message"] and not world.processor.charges


@pytest.mark.parametrize("screen", list(Screen))
def test_a_surcharge_after_approval_is_caught_by_the_amount_binding(world, screen):
    world.merchants["northwind"].surcharge = 600  # the signed checkout shows $34.99; completion asks for $40.99
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": HOME}, hub_intent, screen)
    assert result["status"] == "declined" and result["stage"] == "merchant" and not world.processor.charges


@pytest.mark.parametrize("screen", list(Screen))
def test_a_wrong_store_is_declined_on_either_screen(world, screen):
    result, _ = buy(world, "dealsmart", [("dm-hub", 1)], {"address": HOME}, hub_intent, screen)
    assert result["status"] == "declined" and "not one I chose" in result["message"]


@pytest.mark.parametrize("screen", list(Screen))
def test_an_unwanted_extra_item_is_declined_on_either_screen(world, screen):
    result, _ = buy(world, "northwind", [(HUB.id, 1), ("nw-hub-pro", 1)], {"address": HOME}, hub_intent, screen)
    assert result["status"] == "declined" and "not what I asked for" in result["message"]


# ---- the AP2 example screen really does omit the destination -------------------------------------


def test_the_spec_screen_matches_ap2s_example_fields_and_omits_the_destination(world):
    merchant = world.merchants["northwind"]
    session = merchant.create_session([{"id": HUB.id, "quantity": 1}], fulfillment_details={"address": ATTACKER})
    challenge = merchant.ap2_checkout(session["id"])
    request = request_from_checkout(challenge["checkout_jwt"], merchant.info, challenge["nonce"])
    spec = render(request, Screen.SPEC)
    assert set(spec) == {"merchant_name", "amount", "currency", "items"}  # AP2's example: merchant, amount, item table
    assert "77 Dock St" not in str(spec)                                  # the dock address is nowhere on the screen
    full = render(request, Screen.WITH_DESTINATION)
    assert full["ship_to"]["line_one"] == "77 Dock St"


# ---- the user-signed approval is bound to this exact checkout ------------------------------------


def _approval(world, merchant, session_id, user):
    world.credentials.trust.add(__import__("shipjack.keys", fromlist=["public"]).public(user.key))
    challenge = merchant.ap2_checkout(session_id)
    request = request_from_checkout(challenge["checkout_jwt"], merchant.info, challenge["nonce"])
    approval, _ = DirectSurface(user).approve(request, Screen.SPEC)
    return approval, challenge


def test_an_approval_is_refused_at_another_merchant(world):
    nw, bb = world.merchants["northwind"], world.merchants["bluebird"]
    nw_s = nw.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    user = new_user(hub_intent(world))
    approval, _ = _approval(world, nw, nw_s["id"], user)
    bb_s = bb.create_session([{"id": "bb-hub"}], fulfillment_details={"address": HOME})
    bb.ap2_checkout(bb_s["id"])
    result = bb.complete_session(bb_s["id"], {"handler_id": "direct", "approval": approval})
    assert result["error"]["code"] in ("invalid_credential", "invalid_mandate") and not bb.orders


def test_an_approval_signed_by_an_untrusted_key_is_refused(world):
    merchant = world.merchants["northwind"]
    session = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    user = new_user(hub_intent(world))  # a user whose credential was never enrolled
    challenge = merchant.ap2_checkout(session["id"])
    request = request_from_checkout(challenge["checkout_jwt"], merchant.info, challenge["nonce"])
    approval, _ = DirectSurface(user).approve(request, Screen.SPEC)
    # note: purchase_direct would enroll the key; here we complete directly against the un-enrolled trust list
    result = merchant.complete_session(session["id"], {"handler_id": "direct", "approval": approval})
    assert result["error"]["code"] == "invalid_credential" and not merchant.orders


def test_a_replayed_approval_cannot_pay_twice(world):
    merchant = world.merchants["northwind"]
    s = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    user = new_user(hub_intent(world))
    approval, _ = _approval(world, merchant, s["id"], user)
    first = merchant.complete_session(s["id"], {"handler_id": "direct", "approval": approval})
    assert first["status"] == "completed"
    with pytest.raises(Exception, match="already completed"):  # a finished session cannot be paid again
        merchant.complete_session(s["id"], {"handler_id": "direct", "approval": approval})
    assert len(world.processor.charges) == 1


def test_verify_user_approval_pins_amount_nonce_and_checkout(world):
    merchant = world.merchants["northwind"]
    s = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    user = new_user(hub_intent(world))
    approval, challenge = _approval(world, merchant, s["id"], user)
    trust = world.credentials.trust
    jwt, nonce, total = challenge["checkout_jwt"], challenge["nonce"], 3499
    assert verify_user_approval(approval, trust, aud="northwind", nonce=nonce, checkout_jwt=jwt, amount=total)[1] == []
    assert verify_user_approval(approval, trust, aud="bluebird", nonce=nonce, checkout_jwt=jwt, amount=total)[1]
    assert verify_user_approval(approval, trust, aud="northwind", nonce="other", checkout_jwt=jwt, amount=total)[1]
    assert verify_user_approval(approval, trust, aud="northwind", nonce=nonce, checkout_jwt="h.e.s", amount=total)[1]
    assert verify_user_approval(approval, trust, aud="northwind", nonce=nonce, checkout_jwt=jwt, amount=total + 1)[1]


def test_an_expired_approval_is_refused(world):
    merchant = world.merchants["northwind"]
    s = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    user = new_user(hub_intent(world))
    world.credentials.trust.add(__import__("shipjack.keys", fromlist=["public"]).public(user.key))
    challenge = merchant.ap2_checkout(s["id"])
    request = request_from_checkout(challenge["checkout_jwt"], merchant.info, challenge["nonce"])
    approval, _ = DirectSurface(user, lifetime=-1000).approve(request, Screen.SPEC)
    _, v = verify_user_approval(approval, world.credentials.trust, aud="northwind", nonce=challenge["nonce"],
                                checkout_jwt=challenge["checkout_jwt"], amount=3499)
    assert any("expired" in x for x in v)


# ---- a user who could see the address (a hypothetical better screen) catches everything ----------


def test_a_user_who_sees_every_field_catches_the_redirect_even_on_the_spec_screen(world):
    user = new_user(hub_intent(world))
    user.sees_through_screens = True
    result, _ = buy(world, "northwind", [(HUB.id, 1)], {"address": ATTACKER}, hub_intent, Screen.SPEC, user=user)
    assert result["status"] == "declined" and "ships to" in result["message"]
