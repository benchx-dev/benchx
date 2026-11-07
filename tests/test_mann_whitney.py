"""The second method (comparator.md §4.5). It asks for a different shape of evidence
and nothing of the run's schedule, and lands as a module and a registry line."""

import random

import pytest

from benchx import compare, core
from benchx.methods import mann_whitney
from benchx.methods.mann_whitney import EXACT_LIMIT, rank_test
from test_comparator import BASE, HEAD, invariants, result, run

METHOD = "benchx/mann-whitney/v1"
VALUES = (1.0, 1.02, 0.98, 1.01, 0.99, 1.03)


def evaluate(docs, **config):
    doc = compare.compare(docs, run_key="run", profile="revisions", baseline=BASE, method=METHOD, **config)
    assert core.check(doc, "comparison-document") == []
    return doc


def separated(rounds=4, ratio=1.5):
    return run(rounds=rounds, base_values=VALUES[:rounds], ratio=ratio)


# --- it asks for less of the run ------------------------------------------------

def test_it_records_what_it_found_on_its_own_terms():
    doc = evaluate(separated())
    unit = doc["units"][0]
    assert unit["verdict"] == "regressed" and unit["method"] == {"name": "benchx/mann-whitney", "version": "v1"}
    assert doc["method"]["configuration"] == {"alpha": 0.05}
    assert unit["effect"]["measure"] == "relative-median" and unit["effect"]["value"] == pytest.approx(0.5)
    assert unit["details"]["p_value"] == pytest.approx(2 / 70) and unit["details"]["attempts"] == [4, 4]
    assert unit["details"]["p_method"] == "exact-permutation"
    assert len(unit["used"]) == 8 and unit["excluded"] == []
    assert unit["evidence"]["keys"] == []  # it lines nothing up but the side


def test_a_run_that_did_not_alternate_is_compared_and_says_so():
    """A sequential run: paired-relative refuses it, this method compares it with a caveat."""
    docs = [result(BASE, r, r, VALUES[r]) for r in range(4)] + [result(HEAD, r, 4 + r, VALUES[r] * 1.5) for r in range(4)]
    assert invariants(compare.compare(docs, run_key="run", profile="revisions", baseline=BASE)) == ["alternating-slots"]
    doc = evaluate(docs)
    assert invariants(doc) == [] and doc["units"][0]["verdict"] == "regressed"
    assert doc["units"][0]["evidence"]["slots"] == "not-alternating"
    assert "sides-did-not-alternate" in doc["units"][0]["caveats"]


def test_it_needs_no_equal_attempts_and_no_rounds():
    docs = separated(rounds=5)[:-1]  # one contender attempt short
    for d in docs:
        d["procedure"].pop("round")
    doc = evaluate(docs)
    assert invariants(doc) == [] and doc["units"][0]["details"]["attempts"] == [5, 4]
    assert doc["units"][0]["verdict"] == "regressed"


def test_a_retry_is_just_another_attempt():
    docs = separated()
    retry = result(BASE, 0, 8, 1.0)
    retry["ingest_key"] = "retry"
    assert evaluate(docs + [retry])["units"][0]["details"]["attempts"] == [5, 4]


def test_profile_invariants_still_hold():
    """A selector's invariants are not the method's to waive."""
    docs = separated()
    docs.append(result("c" * 40, 9, 9, 1.0))
    assert invariants(evaluate(docs)) == ["two-sides"]


def test_failed_attempts_are_excluded_as_for_any_method():
    docs = separated(rounds=5)
    docs[0]["measurement"] = {"quantity": docs[0]["measurement"]["quantity"], "status": "error", "reason": "x"}
    doc = evaluate(docs)
    assert doc["units"][0]["details"]["attempts"] == [4, 5]
    assert doc["exclusions"] == [{"result": {"producer": "benchx/gbench-adapter", "ingest_key": docs[0]["ingest_key"]},
                                  "reason": "error: x"}]


# --- its own minimum evidence ---------------------------------------------------

def test_a_design_that_cannot_reach_alpha_does_not_report_no_change():
    """Three attempts per side: the smallest p-value is 0.1, so a 50% change at alpha
    0.05 could never be detected, and "no change detected" would be a false comfort."""
    unit = evaluate(separated(rounds=3))["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-attempts")
    assert unit["details"]["smallest_p"] == pytest.approx(0.1) and "p_value" not in unit["details"]


def test_alpha_is_its_configuration():
    assert evaluate(separated(rounds=3), alpha=0.1)["units"][0]["verdict"] == "regressed"
    with pytest.raises(compare.CompareError, match="k"):
        evaluate(separated(), k=3.0)


def test_a_side_with_every_attempt_failed_is_insufficient():
    docs = separated()
    for d in docs[1::2]:
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error"}
    unit = evaluate(docs)["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-attempts")
    assert unit["details"]["attempts"] == [4, 0]


def test_overlapping_sides_detect_no_change():
    unit = evaluate(separated(rounds=4, ratio=1.0))["units"][0]
    assert unit["verdict"] == "no-change-detected" and unit["details"]["p_value"] == 1.0


# --- it follows the quantity's direction ----------------------------------------

@pytest.mark.parametrize("ratio, direction, verdict", [
    (1.5, "lower-is-better", "regressed"), (0.5, "lower-is-better", "improved"),
    (1.5, "higher-is-better", "improved"), (0.5, "higher-is-better", "regressed"),
    (1.5, None, "changed"),
])
def test_the_label_follows_the_rank_shift_and_the_direction(ratio, direction, verdict):
    docs = separated(ratio=ratio)
    for d in docs:
        if direction:
            d["measurement"]["quantity"]["direction"] = direction
        else:
            d["measurement"]["quantity"].pop("direction")
    assert evaluate(docs)["units"][0]["verdict"] == verdict


def test_a_zero_median_has_a_verdict_but_no_relative_effect():
    docs = run(rounds=4, base_values=(0.0, 0.0, 0.0, 0.0), ratio=1.0)
    for d in docs[1::2]:
        d["measurement"]["observations"] = [1.0]
    unit = evaluate(docs)["units"][0]
    assert unit["verdict"] == "regressed" and "effect" not in unit


def test_deterministic_quantities_are_deferred_here_too():
    docs = separated()
    for d in docs:
        d["measurement"]["quantity"]["deterministic"] = True
    assert evaluate(docs)["units"][0]["reason"] == "deterministic-deferred"


def test_it_renders_in_its_own_terms():
    shown = compare.render(evaluate(separated()))
    assert "REGRESSED (1)" in shown and "p  0.0286  attempts=4+4" in shown and "noise" not in shown


# --- the statistics, against values computed by scipy.stats and brute force -------

@pytest.mark.parametrize("a, b, u, p", [
    # scipy.stats.mannwhitneyu(b, a, method="exact"), no ties
    ([1, 2, 3, 4], [5, 6, 7, 8], 16.0, 0.02857142857142857),
    ([1, 3, 5, 7, 9], [2, 4, 6, 8, 10], 15.0, 0.6904761904761905),
    ([5, 6, 7], [1, 2, 3, 4], 0.0, 0.05714285714285714),
    ([1.5, 2.5, 9.0, 11.0, 13.0, 20.0], [3.0, 4.0, 5.0, 30.0], 12.0, 1.0),
    # brute-force enumeration over midranks, with ties
    ([1, 2, 2, 3], [2, 3, 3, 4], 13.0, 0.2857142857142857),
    ([1, 1, 1, 2], [1, 2, 2, 2], 12.0, 0.4857142857142857),
    ([5, 5, 5], [5, 5, 5], 4.5, 1.0),
])
def test_exact_p_values(a, b, u, p):
    found_u, found_p, how = rank_test(a, b)
    assert how == "exact-permutation"
    assert found_u == u and found_p == pytest.approx(p)


def test_large_samples_use_the_normal_approximation():
    """scipy.stats.mannwhitneyu(b, a, method="asymptotic", use_continuity=True)."""
    a = [float(i) for i in range(0, 42, 2)]
    b = [float(i) + 0.5 for i in range(4, 46, 2)]
    assert len(a) + len(b) > EXACT_LIMIT
    u, p, how = rank_test(a, b)
    assert how == "normal-approximation" and u == 270.0 and p == pytest.approx(0.2177133653142379)


def test_the_exact_and_the_approximate_agree_at_the_limit(monkeypatch):
    """On one sample, so that the switch between them is not a jump in the p-value."""
    rng = random.Random(4)
    a = [rng.gauss(0, 1) for _ in range(20)]
    b = [rng.gauss(0.8, 1) for _ in range(20)]
    _, exact, how = rank_test(a, b)
    assert how == "exact-permutation" and len(a) + len(b) == EXACT_LIMIT
    monkeypatch.setattr(mann_whitney, "EXACT_LIMIT", 0)
    _, normal, how = rank_test(a, b)
    assert how == "normal-approximation" and abs(exact - normal) < 0.005
