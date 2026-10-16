"""The AP2 fulfillment-destination vectors (docs/AP2_FULFILLMENT_VECTORS.md) still hold on the pinned SDK,
and the committed report is exactly what a fresh run produces."""

import json
import pathlib

import pytest

from shipjack.vectors import build_vectors

REPORT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "ap2_fulfillment_vectors.json"


@pytest.fixture(scope="module")
def report():
    return build_vectors()


@pytest.fixture(scope="module")
def by_id(report):
    return {v["id"]: v for v in report["vectors"]}


def test_the_committed_report_matches_a_fresh_run(report):
    assert json.loads(REPORT.read_text()) == report


@pytest.mark.parametrize("vid", ["F1", "F2"])
def test_a_redirected_destination_passes_every_sdk_check_and_stays_a_normative_question(by_id, vid):
    v = by_id[vid]
    sdk = v["sdk"]
    assert sdk["chain_signatures_aud_nonce"] == "verified" and sdk["expected_checkout_hash_passed"]
    assert sdk["destination_in_signed_checkout"] and not sdk["destination_is_a_checkout_model_field"]
    assert sdk["violations"] == []
    assert v["authority"]["status"] == "NORMATIVE_QUESTION"  # passing checks are not proof of authority


@pytest.mark.parametrize("vid", ["F3", "F4"])
def test_the_experimental_constraint_stops_the_redirect_and_allows_the_users_own_destination(by_id, vid):
    v = by_id[vid]
    attack, own = v["attacker_destination"], v["users_own_destination"]
    assert attack["sdk_plus_fulfillment_evaluator"]["violations"]
    assert own["sdk_plus_fulfillment_evaluator"]["violations"] == []
    for case in (attack, own):  # a verifier that doesn't know the type fails it, honest purchases included
        assert case["sdk_unmodified"]["chain_parse"].startswith("rejected")


def test_a_destination_changed_after_signing_is_caught_only_when_the_hash_is_compared(by_id):
    v = by_id["F5"]
    assert v["sdk_caller_passes_no_expected_hash"]["violations"] == []  # AP2 #358
    [mismatch] = v["sdk_hash_derived_from_presented_checkout"]["violations"]
    assert "checkout_hash mismatch" in mismatch
    assert v["case"] == "C1"
