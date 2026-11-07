"""The framework between selectors and methods (comparator.md §2.6, §3, §4): what
counting shows, the layout a method asked for, and who owns which check."""

import copy
import json

import pytest

from benchx import compare, core, methods
from benchx.core import catalog
from benchx.evidence import Assessment, Evidence, Method, NotApplicable, Row, is_complete, pivot, relation, slot_schedule
from benchx.key import Key
from test_comparator import BASE, HEAD, evaluate, invariants, labeled, result, run


def row(side, round_=None, estimate=1.0, name=None, exclusion=None):
    name = name or f"{side}-{round_}-{estimate}"
    return Row({"producer": "p", "ingest_key": name}, side, {"round": round_}, estimate, (estimate,), exclusion)


def evidence(*rows):
    return Evidence("f" * 64, {"name": "w"}, {"name": "wall-time"}, ("baseline", "contender"), tuple(rows))


# --- keys ---------------------------------------------------------------------

@pytest.mark.parametrize("key", [Key("/procedure/round"),
                                 Key("/source/uri", normalize={"rule": "canonical-uri"})])
def test_a_key_round_trips_through_json(key):
    assert Key.from_json(json.loads(json.dumps(key.to_json()))) == key


def test_a_key_reads_normalizes_and_orders():
    key = Key("/source/uri", normalize={"rule": "canonical-uri"})
    assert key.read({"source": {"uri": "https://Example.org/demo.git/"}}) == "https://example.org/demo"
    assert key.read({}) is None
    assert sorted(["6.10", "6.8"], key=Key("/k").sort_key) == ["6.8", "6.10"]


# --- what counting shows ------------------------------------------------------

def test_a_key_on_both_sides_is_crossed_and_one_side_only_is_nested():
    assert relation([row("baseline", 0), row("contender", 0)], "round") == "crossed"
    assert relation([row("baseline", 0), row("contender", 1)], "round") == "nested"
    assert relation([row("baseline"), row("contender")], "round") == "absent"


def test_a_failed_attempt_still_shows_the_design():
    """Every contender attempt failed: the rounds were still crossed, and the
    method sees masked cells rather than a run that was never blocked."""
    rows = [row("baseline", 0), row("contender", 0, estimate=None, exclusion="error")]
    assert relation(rows, "round") == "crossed"
    assert pivot(evidence(*rows), ("round", "side")).cells == [[(rows[0],), ()]]


def test_completeness():
    assert is_complete([row("baseline", 0), row("contender", 0)], "round", ("baseline", "contender"))
    assert not is_complete([row("baseline", 0), row("contender", 0), row("baseline", 1)], "round",
                           ("baseline", "contender"))


@pytest.mark.parametrize("schedule, expected", [
    ([(0, "a"), (1, "b"), (2, "a"), (3, "b")], "alternating"),
    ([(0, "a"), (1, "a"), (2, "b"), (3, "b")], "not-alternating"),
    ([(0, "a"), (0, "b")], "not-alternating"),  # one slot, two sides
    ([(0, "a"), (None, "b")], "absent"),
])
def test_slot_schedule(schedule, expected):
    assert slot_schedule(schedule) == expected


# --- the layout ---------------------------------------------------------------

def test_a_layout_is_over_the_dimensions_a_method_names():
    a0, b0, a1 = row("baseline", 0), row("contender", 0), row("baseline", 1)
    layout = pivot(evidence(a0, b0, a1), ("round", "side"))
    assert layout.coords == {"round": (0, 1), "side": ("baseline", "contender")}
    assert layout.cells == [[(a0,), (b0,)], [(a1,), ()]]


def test_rounds_order_by_value_not_as_strings():
    rows = [row(side, r) for r in (10, 2, 1) for side in ("baseline", "contender")]
    assert pivot(evidence(*rows), ("round", "side")).coords["round"] == (1, 2, 10)


def test_one_dimension_holds_every_attempt_of_a_side():
    a0, a1, b0 = row("baseline", 0), row("baseline", 1), row("contender", 0)
    assert pivot(evidence(a0, a1, b0), ("side",)).cells == [(a0, a1), (b0,)]


@pytest.mark.parametrize("rows, match", [
    ([row("baseline"), row("contender")], "no result has one"),
    ([row("baseline", 0), row("contender", 1)], "no value of round appears on more than one side"),
])
def test_a_unit_that_cannot_be_laid_out_says_why(rows, match):
    with pytest.raises(NotApplicable, match=match):
        pivot(evidence(*rows), ("round", "side"))


# --- units are series (decision 7) --------------------------------------------

def test_sides_that_disagree_are_two_series_and_refused():
    docs = run()
    for d in docs[1::2]:
        d["coordinates"]["subject"]["configuration"] = {"build_type": "Debug"}
    doc = evaluate(docs)
    assert invariants(doc) == ["sides-agree"] and doc["missing"] == [] and doc["units"] == []
    assert doc["failed_invariants"][0]["owner"] == "selector"


def test_a_variant_on_one_side_is_one_series_and_merely_missing():
    """The same two one-sided groups as a disagreement, by shape: what differs is
    that they are one workload variant or two."""
    docs = run()
    for slot, (side, value) in enumerate(((BASE, 1.0), (HEAD, 1.1)), start=6):
        docs.append(result(side, 3, slot, value, workload=f"BM_only_{slot}"))
    doc = evaluate(docs)
    assert invariants(doc) == []
    assert {(m["workload"]["name"], m["side"]) for m in doc["missing"]} == {("BM_only_6", "contender"),
                                                                            ("BM_only_7", "baseline")}


def test_one_unit_has_one_series_and_keeps_each_sides_policy_series():
    unit = evaluate(run())["units"][0]
    assert set(unit["side_series"]) == {"baseline", "contender"} and len(unit["series"]) == 64


# --- who owns which check (decision 6) ----------------------------------------

def test_a_method_owns_what_its_statistics_assume():
    docs = run()
    docs[1]["procedure"]["slot"], docs[2]["procedure"]["slot"] = 2, 1  # a, a, b, b, a, b
    doc = evaluate(docs)
    assert invariants(doc) == ["alternating-slots"] and doc["failed_invariants"][0]["owner"] == "method"


def test_alternation_is_judged_over_the_run_not_the_unit():
    """The run alternates. BM_Partial alone reads a, b, b, a, because it skipped
    two attempts, and that says nothing about the schedule."""
    docs, slot = [], 0
    for r in range(3):
        for side, value in ((BASE, 1.0 + r), (HEAD, (1.0 + r) * 1.1)):
            docs.append(result(side, r, slot, value))
            if (r, side) not in ((1, BASE), (2, HEAD)):
                docs.append(result(side, r, slot, value, workload="BM_Partial"))
            slot += 1
    doc = evaluate(docs)
    assert invariants(doc) == [] and {u["workload"]["name"] for u in doc["units"]} == {"BM_W", "BM_Partial"}
    assert {u["evidence"]["slots"] for u in doc["units"]} == {"alternating"}


def test_two_usable_results_in_a_cell_are_refused_not_overwritten():
    """Both sides retried round 0. The old comparator kept whichever sorted last,
    so a base of 100 silently replaced 1.0 and turned +10% into -26%."""
    docs = run()
    retries = [result(BASE, 0, 6, 100.0), result(HEAD, 0, 7, 1.1)]
    for d, name in zip(retries, ("retry-a", "retry-b")):
        d["ingest_key"] = name
    doc = evaluate(docs + retries)
    assert invariants(doc) == ["duplicate-points"] and doc["units"] == []
    assert doc["failed_invariants"][0]["workload"] == {"name": "BM_W"}


def test_a_failed_attempt_and_its_retry_are_one_usable_result_in_the_cell():
    """Schema §5.3: a retry keeps its round, so that pairing survives."""
    docs = run()
    failed = copy.deepcopy(docs[:2])
    for d in failed:
        d["ingest_key"] += ":failed"
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
    doc = evaluate(docs + failed)
    assert invariants(doc) == [] and doc["units"][0]["details"]["rounds"] == 3


@pytest.mark.parametrize("change, detail", [
    (lambda d: d["procedure"].pop("round"), "no result has one"),
    (lambda d: d["procedure"].update(round=d["procedure"]["round"] + 10 * (d["revision"]["key"] == HEAD)),
     "no value of round appears on more than one side"),
])
def test_a_method_that_does_not_apply_says_so_rather_than_counting_no_rounds(change, detail):
    docs = run()
    for d in docs:
        change(d)
    doc = evaluate(docs)
    assert invariants(doc) == ["layout"] and doc["units"] == []
    assert detail in doc["failed_invariants"][0]["detail"]


def test_results_with_no_round_are_not_paired_with_each_other():
    """The old comparator treated a missing round as a round named None."""
    docs = run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    docs[0]["procedure"].pop("round")
    docs[1]["procedure"].pop("round")
    unit = evaluate(docs)["units"][0]
    assert unit["details"]["rounds"] == 3
    assert {e["reason"] for e in unit["excluded"]} == {"no round"} and len(unit["excluded"]) == 2


# --- the outcome ---------------------------------------------------------------

def test_every_result_is_used_or_excluded_exactly_once():
    docs = run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    docs[2]["measurement"] = {"quantity": docs[2]["measurement"]["quantity"], "status": "error", "reason": "x"}
    unit = evaluate(docs)["units"][0]
    used = {u["ingest_key"] for u in unit["used"]}
    excluded = {e["result"]["ingest_key"]: e["reason"] for e in unit["excluded"]}
    assert not used & set(excluded) and used | set(excluded) == {d["ingest_key"] for d in docs}
    assert excluded == {docs[2]["ingest_key"]: "error: x", docs[3]["ingest_key"]: "unpaired"}


def test_the_noise_says_what_it_is_and_records_its_degrees_of_freedom():
    details = evaluate(run(rounds=5, base_values=(1.0, 2.0, 3.0, 4.0, 5.0)))["units"][0]["details"]
    assert details["noise_kind"] == "standard-error" and details["degrees_of_freedom"] == 4
    assert details["threshold"] == pytest.approx(3.0 * details["noise"])


def test_the_evidence_summary_says_how_each_key_relates_to_the_side():
    unit = evaluate(run())["units"][0]
    assert unit["evidence"]["keys"] == [{"name": "round", "key": {"pointer": "/procedure/round", "order": "natural"},
                                         "relation": "crossed", "complete": True}]
    assert unit["method"] == {"name": "benchx/paired-relative", "version": "v1"}


def test_the_method_and_selector_are_recorded_even_with_no_units():
    docs = run()
    docs.append(result("c" * 40, 3, 6, 1.0))
    doc = evaluate(docs)
    assert doc["units"] == [] and doc["method"]["configuration"] == {"k": 3.0, "min_rounds": 3}
    assert doc["selector"]["params"] == {"run_key": "run", "profile": "revisions", "baseline": BASE}


def test_one_comparison_has_one_document_however_the_baseline_was_abbreviated():
    full = compare.compare(run(), run_key="run", profile="revisions", baseline=BASE)
    assert compare.compare(run(), run_key="run", profile="revisions", baseline=BASE[:6]) == full


def test_the_same_inputs_give_the_same_document_in_any_order():
    docs = run()
    assert evaluate(docs) == evaluate(list(reversed(copy.deepcopy(docs))))


def test_labels_reach_the_selector_params():
    doc = compare.compare(labeled(run()), run_key="run", profile="environments", baseline="plain", label="build")
    assert doc["selector"]["params"]["label"] == "build"


# --- a method's own schemas -----------------------------------------------------

def test_an_unknown_method_is_refused():
    with pytest.raises(compare.CompareError, match="not in the implementation"):
        compare.compare(run(), run_key="run", profile="revisions", baseline=BASE, method="nobody/v1")


def test_configuration_is_checked_against_the_methods_schema():
    with pytest.raises(compare.CompareError, match="alpha"):
        compare.compare(run(), run_key="run", profile="revisions", baseline=BASE, alpha=0.05)
    with pytest.raises(compare.CompareError, match="benchx/paired-relative/v1"):
        compare.compare(run(), run_key="run", profile="revisions", baseline=BASE, k="three")


class Careless(Method):
    name, version, dims = "test/careless", "v1", ("side",)
    details_schema = {"type": "object", "additionalProperties": False}

    def assess(self, layout, evidence, config):
        return Assessment("no-change-detected", details={"undeclared": 1})


def test_details_are_checked_against_the_methods_schema():
    from jsonschema import ValidationError

    from benchx.run_profile import RUN_PROFILE
    selection = RUN_PROFILE.select(run(), run_key="run", profile="revisions", baseline=BASE)
    with pytest.raises(ValidationError, match="undeclared"):
        compare.evaluate(selection, Careless(), {})


class TakesNothing(Method):
    """Uses nothing and says nothing: the framework still accounts for every result."""
    name, version, dims = "test/takes-nothing", "v1", ("side",)

    def assess(self, layout, evidence, config):
        return Assessment("indeterminate", reason="declined")


def test_a_result_a_method_does_not_mention_is_still_accounted_for():
    from benchx.run_profile import RUN_PROFILE
    selection = RUN_PROFILE.select(run(), run_key="run", profile="revisions", baseline=BASE)
    doc = compare.evaluate(selection, TakesNothing(), {})
    assert core.check(doc, "comparison-document") == []
    unit = doc["units"][0]
    assert unit["used"] == [] and {e["reason"] for e in unit["excluded"]} == {"not-used"} and len(unit["excluded"]) == 6


def test_the_default_method_is_registered():
    assert methods.DEFAULT in methods.METHODS and catalog.COMPARISON_DOCUMENT.endswith("/0.2.0")


# --- a selector's no-verdict rule (UC-02 §10) holds for every method ----------------------------

def _variants_with_a_failed_attempt():
    from test_comparator import fail, variants_run
    docs = variants_run(rounds=5, base_values=(1.0, 2.0, 3.0, 4.0, 5.0), ratio=1.5)
    fail(docs[2], "error")
    return docs


@pytest.mark.parametrize("method", sorted(methods.METHODS))
def test_a_failed_attempt_under_variants_leaves_no_verdict_whatever_the_method(method):
    doc = compare.compare(_variants_with_a_failed_attempt(), run_key="run", profile="variants",
                          baseline="'baseline'", parameter="role", method=method)
    assert core.check(doc, "comparison-document") == []
    [unit] = doc["units"]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "side-failed") and "effect" not in unit


class Ignores(Method):
    """Gives a verdict whatever the selector says."""
    name, version, dims = "test/ignores", "v1", ("side",)

    def assess(self, layout, evidence, config):
        return Assessment("regressed", effect={"measure": "relative", "value": 0.5})


def test_a_method_that_ignores_the_selectors_rule_is_a_bug_not_a_verdict():
    from benchx.run_profile import RUN_PROFILE
    selection = RUN_PROFILE.select(_variants_with_a_failed_attempt(), run_key="run", profile="variants",
                                   baseline="'baseline'", parameter="role")
    with pytest.raises(AssertionError, match="side-failed"):
        compare.evaluate(selection, Ignores(), {})


def test_the_varied_parameter_is_recorded_with_the_selector():
    from test_comparator import variants_run
    doc = compare.compare(variants_run(), run_key="run", profile="variants", baseline="'baseline'", parameter="role")
    assert doc["selector"]["params"] == {"run_key": "run", "profile": "variants", "baseline": "'baseline'",
                                         "parameter": "role"}
