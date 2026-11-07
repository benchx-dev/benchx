"""A second method lands as one module (comparator.md §4.5): the prototype in
mann_whitney_prototype.py asks for other dimensions than the default and nothing of
the run's schedule, and the framework runs it unchanged."""

import pytest

from benchx import compare, core
from benchx.run_profile import RUN_PROFILE
from mann_whitney_prototype import METHOD, rank_test
from test_comparator import BASE, HEAD, invariants, result, run

VALUES = (1.0, 1.02, 0.98, 1.01, 0.99)


def evaluate(docs, **config):
    selection = RUN_PROFILE.select(docs, run_key="run", profile="revisions", baseline=BASE)
    doc = compare.evaluate(selection, METHOD, compare._configure(METHOD, config))
    assert core.check(doc, "comparison-document") == []
    return doc


def test_a_run_that_did_not_alternate_is_refused_by_the_default_and_compared_by_this_one():
    docs = [result(BASE, r, r, VALUES[r]) for r in range(4)] + [result(HEAD, r, 4 + r, VALUES[r] * 1.5) for r in range(4)]
    assert invariants(compare.compare(docs, run_key="run", profile="revisions", baseline=BASE)) == ["alternating-slots"]
    unit = evaluate(docs)["units"][0]
    assert unit["verdict"] == "regressed" and unit["details"]["p_value"] == pytest.approx(2 / 70)
    assert unit["effect"] == {"measure": "relative-median", "value": pytest.approx(0.5)}
    assert "sides-did-not-alternate" in unit["caveats"]


def test_a_design_that_cannot_reach_alpha_is_indeterminate():
    """Three attempts per side: the smallest two-sided p-value is 0.1."""
    unit = evaluate(run(rounds=3, base_values=VALUES[:3], ratio=1.5))["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-attempts")


@pytest.mark.parametrize("a, b, u, p", [
    ([1, 2, 3, 4], [5, 6, 7, 8], 16.0, 0.02857142857142857),  # scipy.stats.mannwhitneyu(b, a, method="exact")
    ([1, 2, 2, 3], [2, 3, 3, 4], 13.0, 0.2857142857142857),   # brute force over midranks, with ties
])
def test_exact_p_values(a, b, u, p):
    assert rank_test(a, b) == (u, pytest.approx(p))
