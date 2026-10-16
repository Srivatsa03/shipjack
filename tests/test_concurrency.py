"""Two requests at once: a retry, a double click, two workers. Nothing may be paid twice.

The sleeps widen the window between "check" and "act" so a missing lock fails reliably instead of
once in a thousand runs.
"""

import threading
import time

from conftest import HOME, HUB
from rig import Rig
from shipjack.mandates import digest
from shipjack.psp import PaymentError

THREADS = 12


def run_together(fn, n=THREADS):
    results, barrier = [], threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            results.append(("ok", fn(i)))
        except Exception as exc:  # noqa: BLE001  (the test counts outcomes)
            results.append(("err", exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_one_token_pays_once_when_redeemed_by_many_threads_at_once(world, monkeypatch):
    r = Rig(world)
    cp = world.credentials
    nonce = cp.nonce()
    token = cp.issue(r.payment_mandate(nonce=nonce), nonce, r.deleg.open_checkout_hash)
    real = cp.redeem

    def slow_redeem(t):
        scope = real(t)
        time.sleep(0.02)  # the window between "token is unused" and "mark it used"
        return scope

    monkeypatch.setattr(cp, "redeem", slow_redeem)
    results = run_together(lambda i: world.processor.charge_token(r.merchant.account, token, r.total, "USD",
                                                                  digest(r.checkout_jwt), r.deleg.open_checkout_hash))
    assert len(world.processor.charges) == 1
    assert sum(1 for kind, _ in results if kind == "ok") == 1
    assert all(isinstance(v, PaymentError) for kind, v in results if kind == "err")


def test_one_nonce_issues_one_token_when_presented_by_many_threads_at_once(world, monkeypatch):
    r = Rig(world)
    cp = world.credentials
    nonce = cp.nonce()
    token = r.payment_mandate(nonce=nonce)
    real = cp._nonces.__contains__

    class SlowSet(set):
        def __contains__(self, item):
            found = real(item) if False else set.__contains__(self, item)
            time.sleep(0.02)
            return found

    cp._nonces = SlowSet(cp._nonces)
    results = run_together(lambda i: cp.issue(token, nonce, r.deleg.open_checkout_hash))
    assert sum(1 for kind, _ in results if kind == "ok") == 1


def test_two_card_completions_of_one_session_charge_once(world, monkeypatch):
    merchant = world.merchants["northwind"]
    session = merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    real = world.processor.charge_card

    def slow_charge(*a, **kw):
        time.sleep(0.02)
        return real(*a, **kw)

    monkeypatch.setattr(world.processor, "charge_card", slow_charge)
    pay = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}
    run_together(lambda i: merchant.complete_session(session["id"], pay))
    assert len(world.processor.charges) == 1 and len(merchant.orders) == 1


def test_many_sessions_created_at_once_get_distinct_ids(world):
    merchant = world.merchants["northwind"]
    results = run_together(lambda i: merchant.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})["id"],
                           n=40)
    ids = [v for kind, v in results if kind == "ok"]
    assert len(ids) == 40 and len(set(ids)) == 40
