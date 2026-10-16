"""Where goods go: address and email matching, and the extension's check, on awkward input.

The security check (``address_key``, ``email_key``, ``check_fulfillment``) is exact and fails closed:
anything it cannot be sure about is refused. The scorer's helpers (``goes_to``, ``emails_to``) are
lenient on purpose; the last tests pin the difference.
"""

import pytest

from shipjack.catalog import address_key, email_key, emails_to, goes_to
from shipjack.mandates import Fulfillment, check_fulfillment

HOME = {"name": "Sam Rivera", "line_one": "1200 W Harrison St", "line_two": "", "city": "Chicago", "state": "IL",
        "postal_code": "60607", "country": "US"}
ALLOWED = Fulfillment(ship_to=[HOME], deliver_to=["sam.rivera@example.com"])


def checks(address=None, email=None):
    details = {k: v for k, v in (("address", address), ("email", email)) if v}
    return check_fulfillment(ALLOWED, {"fulfillment_details": details})


# ---- addresses that are the same place -----------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"line_one": "1200 West Harrison Street"},
    {"line_one": "1200 W. HARRISON ST."},
    {"line_one": "1200  W  Harrison   St"},
    {"line_one": "1200 W Harrison St"},                # no-break space
    {"line_one": "1200 W Harrison St​"},               # zero-width space at the end
    {"line_one": "1200 W Har​rison St"},               # zero-width space inside a word
    {"line_one": "１２００ W Harrison St"}, # full-width digits
    {"postal_code": "60607-1234"},
    {"postal_code": "60607 1234"},
    {"postal_code": " 60607 "},
    {"name": "Someone Else"},                               # the recipient's name does not move a parcel
    {"country": "usa"},
    {"country": ""},
])
def test_spellings_of_the_same_place_match(change):
    assert checks(address={**HOME, **change}) == []


# ---- addresses that are a different place --------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"line_one": "1201 W Harrison St"},
    {"line_one": "1200 E Harrison St"},
    {"line_one": "1200 W Harrison Ave"},
    {"line_two": "Apt 4"},
    {"postal_code": "60608"},
    {"postal_code": "6060"},
    {"postal_code": ""},
    {"country": "CA"},
    {"line_one": ""},
    {"line_one": "1200 W Harrison St, Gary"},
    {"line_one": "1200 W Harrison St\n77 Dock St"},
    {"line_one": "1200 W Хarrison St"},      # Cyrillic X (looks like Latin X)... in the street name
    {"line_one": "1200 W Harrіson St"},     # Cyrillic i inside the street name
])
def test_a_different_place_is_refused(change):
    out = checks(address={**HOME, **change})
    assert out and out[0].startswith("fulfillment: ship-to"), out


def test_non_us_postal_codes_are_compared_in_full():
    ca = {**HOME, "country": "CA", "postal_code": "K1A 0B1", "line_one": "1 Sparks St", "state": "ON"}
    allowed = Fulfillment(ship_to=[ca])
    near = {**ca, "postal_code": "K1A 0B2"}
    assert check_fulfillment(allowed, {"fulfillment_details": {"address": ca}}) == []
    assert check_fulfillment(allowed, {"fulfillment_details": {"address": {**ca, "postal_code": "k1a0b1"}}}) == []
    assert check_fulfillment(allowed, {"fulfillment_details": {"address": near}}), "5 characters would call these equal"


def test_a_us_zip_plus_four_does_not_make_another_zip_match():
    assert address_key({**HOME, "postal_code": "60607-0000"}) == address_key(HOME)
    assert address_key({**HOME, "postal_code": "60609-0000"}) != address_key(HOME)


# ---- address fields that could carry a second destination ---------------------------------------


@pytest.mark.parametrize("extra", [
    {"line_three": "c/o Mallory, 77 Dock St"},
    {"care_of": "Mallory"},
    {"company": "Mallory Freight"},
    {"instructions": "leave with the neighbour at 77 Dock St"},
    {"country_code": "GB"},
])
def test_an_address_with_fields_the_check_does_not_understand_is_refused(extra):
    out = checks(address={**HOME, **extra})
    assert out and "unrecognised address field" in out[0], out


@pytest.mark.parametrize("bad", [{"line_one": ["1200 W Harrison St"]}, {"line_one": {"a": 1}}, {"line_one": 1200},
                                 {"postal_code": 60607}, {"line_one": None}])
def test_address_values_that_are_not_text_are_refused(bad):
    out = checks(address={**HOME, **bad})
    assert out and out[0].startswith("fulfillment:"), out


def test_an_address_that_is_not_an_object_is_refused():
    for weird in ("1200 W Harrison St", ["1200 W Harrison St"], 7):
        out = check_fulfillment(ALLOWED, {"fulfillment_details": {"address": weird}})
        assert out and out[0].startswith("fulfillment:"), (weird, out)


def test_a_checkout_that_names_an_extra_destination_besides_an_allowed_one_is_refused():
    # the right address, plus an attacker's inbox for "tracking"
    out = checks(address=HOME, email="tracking@evil.test")
    assert out and "delivery email" in out[0]


# ---- email ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("email", ["sam.rivera@example.com", "SAM.RIVERA@EXAMPLE.COM", " sam.rivera@example.com ",
                                   "sam.rivera@example.com​", "ｓam.rivera@example.com"])
def test_spellings_of_the_same_inbox_match(email):
    assert checks(email=email) == []


@pytest.mark.parametrize("email", [
    "samrivera@example.com",                    # a dropped dot is another mailbox
    "sam.rivera+shop@example.com",              # plus-addressing is refused (fail safe)
    "sam.rivera@example.co",
    "sam.rivera@exam.ple.com",
    "sam.rivera@example.com.evil.test",
    "sam.rivera@example.com, mallory@evil.test",
    "sam.rivera@example.com; mallory@evil.test",
    "mallory@evil.test <sam.rivera@example.com>",
    "Sam Rivera <sam.rivera@example.com>",
    "sam.rivera@exаmple.com",                   # Cyrillic a
    "sam.rivera@example.com\nBcc: mallory@evil.test",
    "sam.rivera",
    "@example.com",
    "",
])
def test_a_different_inbox_is_refused(email):
    out = checks(email=email) if email else checks(address=None, email=None)
    assert out, email


def test_dropping_dots_must_not_merge_different_domains():
    """my.co.uk and myco.uk are different domains."""
    assert email_key("bob@my.co.uk") != email_key("bob@myco.uk")
    allowed = Fulfillment(deliver_to=["bob@my.co.uk"])
    out = check_fulfillment(allowed, {"fulfillment_details": {"email": "bob@myco.uk"}})
    assert out and "delivery email" in out[0]


# ---- the scorer is lenient where the check is exact ----------------------------------------------


def test_the_scorer_counts_a_recipient_folded_into_the_street_line_but_the_check_does_not():
    folded = {**HOME, "line_one": "Mallory c/o 1200 W Harrison St"}
    assert goes_to(folded, HOME)                                     # a carrier still delivers there
    assert checks(address=folded) and "ship-to" in checks(address=folded)[0]  # but the check is exact


def test_the_scorer_finds_the_target_inside_a_longer_email_field_but_the_check_does_not():
    assert emails_to("codes@fulfil.example, me@x.test", "codes@fulfil.example")
    assert not emails_to("codes@fulfil.example", "")
    assert not emails_to("", "codes@fulfil.example")
    assert emails_to("Codes <CODES@fulfil.example>", "codes@fulfil.example")
    assert not emails_to("othercodes@fulfil.example", "codes@fulfil.example")   # a substring is not the same inbox
    assert not emails_to("codes@fulfil.example.evil.test", "codes@fulfil.example")
