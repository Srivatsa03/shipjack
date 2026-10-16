"""Random and mutated input, seeded so a failure can be replayed.

Properties, not examples: a verifier never raises on garbage; a mutated token is either refused or
means exactly what the original meant; the matcher accepts an address only when it is the allowed one.
"""

import random

import pytest
from fastapi.testclient import TestClient

from conftest import HOME, HUB
from rig import Rig
from shipjack.catalog import address_key, email_key
from shipjack.http import create_app
from shipjack.mandates import Fulfillment, check_fulfillment
from shipjack.merchant import ACP_API_VERSION

SEEDS = range(8)


def mutations(token: str, rng: random.Random, n=40):
    """Broken variants of a ``a~b~c~~d~e`` token: flipped characters, cuts, repeats, swapped and dropped parts."""
    parts = token.split("~")
    for _ in range(n):
        kind = rng.choice(["flip", "cut", "repeat", "swap", "drop", "dup-sep", "insert", "case"])
        t = list(token)
        i = rng.randrange(len(t))
        if kind == "flip":
            t[i] = rng.choice("AbZ09-_.~=")
            yield "".join(t)
        elif kind == "cut":
            yield token[: rng.randrange(1, len(token))]
        elif kind == "repeat":
            yield token + token[rng.randrange(len(token)):]
        elif kind == "swap":
            p = parts[:]
            a, b = rng.randrange(len(p)), rng.randrange(len(p))
            p[a], p[b] = p[b], p[a]
            yield "~".join(p)
        elif kind == "drop":
            p = parts[:]
            del p[rng.randrange(len(p))]
            yield "~".join(p)
        elif kind == "dup-sep":
            yield token.replace("~~", "~~~~", 1) if "~~" in token else token + "~~"
        elif kind == "insert":
            t.insert(i, rng.choice(["~", "~~", ".", " ", "\n", "​", "é"]))
            yield "".join(t)
        else:
            yield token.swapcase()


@pytest.mark.parametrize("seed", SEEDS)
def test_a_mutated_checkout_mandate_is_refused_or_means_the_same(world, seed):
    r = Rig(world)
    good = r.checkout_mandate()
    original, _ = r.verify_checkout(good)
    rng = random.Random(seed)
    for bad in mutations(good, rng):
        closed, violations = r.verify_checkout(bad)            # must not raise
        if not violations:
            assert closed.checkout_hash == original.checkout_hash and closed.checkout_jwt == original.checkout_jwt


@pytest.mark.parametrize("seed", SEEDS)
def test_a_mutated_payment_mandate_is_refused_or_means_the_same(world, seed):
    r = Rig(world)
    good = r.payment_mandate()
    original, _ = r.verify_payment(good)
    rng = random.Random(100 + seed)
    for bad in mutations(good, rng):
        closed, violations = r.verify_payment(bad)
        if not violations:
            assert closed.transaction_id == original.transaction_id
            assert closed.payment_amount == original.payment_amount and closed.payee == original.payee


@pytest.mark.parametrize("seed", SEEDS)
def test_random_text_is_never_a_chain(world, seed):
    r = Rig(world)
    rng = random.Random(200 + seed)
    alphabet = "abcXYZ019-_.~= \n​аé{}\"'"
    for _ in range(60):
        junk = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 200)))
        assert r.verify_checkout(junk)[1] and r.verify_payment(junk)[1]


@pytest.mark.parametrize("seed", SEEDS)
def test_a_mutated_mandate_never_completes_a_purchase(world, seed):
    r = Rig(world)
    token = r.checkout_mandate()
    rng = random.Random(300 + seed)
    for bad in mutations(token, rng, n=15):
        merchant_nonce_before = r.merchant.sessions[r.session_id].nonce
        result = r.merchant.complete_session(r.session_id, {"handler_id": "ap2", "checkout_mandate": bad,
                                                            "payment_token": "tok_none"})
        assert result.get("status") != "completed" and not r.merchant.orders and not world.processor.charges
        r.merchant.ap2_checkout(r.session_id)            # the nonce is burned by an attempt; ask for a new one
        r.checkout_jwt = r.merchant.sessions[r.session_id].checkout_jwt
        assert merchant_nonce_before != r.merchant.sessions[r.session_id].nonce


def random_json(rng, depth=0):
    kinds = ["int", "float", "str", "none", "bool", "list", "dict"] if depth < 3 else ["int", "str", "none"]
    kind = rng.choice(kinds)
    if kind == "int":
        return rng.choice([0, 1, -1, 2, 10**9, 10**30, rng.randrange(-5, 5)])
    if kind == "float":
        return rng.choice([0.0, 1.0, 1.5, -2.5, 1e308])
    if kind == "str":
        return rng.choice(["", "nw-hub-7", "card", "ap2", "x" * 300, "​", "null", "{}", "tok_x", "sam@example.com"])
    if kind == "none":
        return None
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "list":
        return [random_json(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    keys = ["items", "id", "quantity", "fulfillment_details", "address", "line_one", "postal_code", "email", "buyer",
            "payment_data", "handler_id", "instrument", "credential", "number", "checkout_mandate", "payment_token"]
    return {rng.choice(keys): random_json(rng, depth + 1) for _ in range(rng.randrange(0, 5))}


@pytest.mark.parametrize("seed", SEEDS)
def test_random_json_never_crashes_the_api_or_moves_money(world, seed):
    app = create_app(world.merchants["northwind"], {"t": "caller"})
    api = TestClient(app, raise_server_exceptions=False)
    rng = random.Random(400 + seed)
    headers = lambda n: {"Authorization": "Bearer t", "API-Version": ACP_API_VERSION, "Idempotency-Key": f"k{seed}-{n}"}  # noqa: E731
    good = api.post("/checkout_sessions", json={"items": [{"id": HUB.id, "quantity": 1}],
                                                "fulfillment_details": {"address": HOME}}, headers=headers("g")).json()["id"]
    for n in range(60):
        path = rng.choice(["/checkout_sessions", f"/checkout_sessions/{good}", f"/checkout_sessions/{good}/complete",
                           f"/checkout_sessions/{good}/ap2", "/checkout_sessions/cs_nope/complete"])
        r = api.post(path, json=random_json(rng), headers=headers(n))
        assert r.status_code < 500, (path, r.status_code, r.text[:200])
    assert not world.processor.charges


FUZZ_ALPHABET = "abcdefXYZ0123456789 .,#-_/​ １аі\n\t@+'\"<>;()[]é"


@pytest.mark.parametrize("seed", SEEDS)
def test_the_matcher_only_accepts_the_allowed_place(seed):
    rng = random.Random(500 + seed)
    allowed = Fulfillment(ship_to=[HOME], deliver_to=["sam.rivera@example.com"])
    fields = ["name", "line_one", "line_two", "city", "state", "postal_code", "country"]
    accepted = 0
    for _ in range(400):
        address = {k: (HOME[k] if rng.random() < 0.5 else "".join(rng.choice(FUZZ_ALPHABET)
                                                                  for _ in range(rng.randrange(0, 12))))
                   for k in fields}
        out = check_fulfillment(allowed, {"fulfillment_details": {"address": address}})        # must not raise
        if not out:
            accepted += 1
            assert address_key(address) == address_key(HOME)
    junk = "".join(rng.choice(FUZZ_ALPHABET) for _ in range(rng.randrange(0, 30)))
    assert check_fulfillment(allowed, {"fulfillment_details": {"email": junk}})                # never accepted at random
    assert accepted >= 0


@pytest.mark.parametrize("seed", SEEDS)
def test_email_key_never_raises_and_only_ever_names_one_mailbox(seed):
    rng = random.Random(600 + seed)
    for _ in range(500):
        s = "".join(rng.choice(FUZZ_ALPHABET) for _ in range(rng.randrange(0, 30)))
        key = email_key(s)
        assert key == "" or (key.count("@") == 1 and not any(c in key for c in " ,;<>()[]\"'\n\t"))
    for value in (None, 7, [], {}, b"x", float("nan")):
        assert email_key(value) == "" or isinstance(email_key(value), str)
