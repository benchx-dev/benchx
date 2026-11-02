"""Comparator invariants and method, on hand-built results whose values the test controls.

Each test changes one thing from a clean three-round `revisions` run and
checks that the comparator notices (prototype-ablation.md §6).
"""

import copy

import pytest

from benchx import compare, core

BASE, HEAD = "a" * 40, "b" * 40


def result(side, round_, slot, value, workload="BM_W", **changes):
    doc = {
        "producer": {"name": "benchx/gbench-adapter"},
        "ingest_key": f"run:{workload}:wall-time:slot-{slot}",
        "source": {"uri": "https://example.org/demo"},
        "revision": {"key": side},
        "coordinates": {
            "workload": {"name": workload},
            "subject": {"name": "demo", "components": [{"role": "primary"}], "configuration": {"build_type": "Release"}},
            "protocol": {"harness": {"name": "google-benchmark"}},
            "environment": {"schema_version": "machine/v1", "identity": {"runner": "host"},
                            "system": {"os": {"kernel": "6.8"}}},
        },
        "measurement": {"quantity": {"name": "wall-time", "unit": "s", "direction": "lower-is-better"},
                        "status": "success", "observations": [value]},
        "procedure": {"round": round_, "slot": slot},
        "provenance": {"run_key": "run", "subject_dirty": "clean", "benchmark_dirty": "clean", "subject_tree": side},
    }
    for path, value_ in changes.items():
        target = doc
        *parents, leaf = path.split("__")
        for part in parents:
            target = target[part]
        target[leaf] = value_
    return doc


def run(rounds=3, base_values=(1.0, 2.0, 3.0), ratio=1.1):
    """Alternating base/head attempts; the head is `ratio` times the base in each round."""
    docs, slot = [], 0
    for r in range(rounds):
        for side, value in ((BASE, base_values[r]), (HEAD, base_values[r] * ratio)):
            docs.append(result(side, r, slot, value))
            slot += 1
    return docs


def evaluate(docs, **kwargs):
    doc = compare.compare(docs, run_key="run", profile="revisions", baseline=BASE, **kwargs)
    assert core.check(doc, "comparison-document") == []
    return doc


def invariants(doc):
    return [f["invariant"] for f in doc["failed_invariants"]]


def test_clean_run():
    doc = evaluate(run())
    assert invariants(doc) == [] and doc["units"][0]["verdict"] == "regressed"


def test_pairs_by_round_not_by_arrival():
    """C13: the head's rounds arrive in reverse; pairing by round still sees +10% exactly."""
    docs = run()
    heads = [d for d in docs if d["revision"]["key"] == HEAD]
    for d, r in zip(heads, (2, 1, 0)):
        d["procedure"]["round"] = r
        d["measurement"]["observations"] = [(r + 1) * 1.1]
    unit = evaluate(docs)["units"][0]
    assert unit["effect"] == pytest.approx(0.1) and unit["noise"] == pytest.approx(0, abs=1e-12)


def test_equal_attempts():
    """C4: a unit with fewer attempts on one side gets no verdict."""
    docs = run()
    docs.append(result(BASE, 3, 6, 4.0, workload="BM_W"))
    docs.append(result(HEAD, 3, 7, 4.4, workload="BM_Other"))
    doc = evaluate(docs)
    assert invariants(doc) == ["equal-attempts"]
    assert [u["workload"] for u in doc["units"]] == []  # BM_W failed; BM_Other is missing on base
    assert doc["missing"] == [{"workload": {"name": "BM_Other"}, "quantity": "wall-time", "side": "baseline"}]


def test_two_sides():
    """C5: a third tree in the run."""
    docs = run()
    docs.append(result("c" * 40, 3, 6, 1.0))
    assert invariants(evaluate(docs)) == ["two-sides"]


def test_sides_agree():
    """C1: the revisions sides must share subject configuration."""
    docs = run()
    for d in docs[1::2]:
        d["coordinates"]["subject"]["configuration"] = {"build_type": "Debug"}
    doc = evaluate(docs)
    assert invariants(doc) == ["sides-agree"]
    assert doc["failed_invariants"][0]["detail"] == "/coordinates/subject/configuration/build_type differ"


def test_anything_not_named_must_agree():
    """A fact no projection names, such as a new runtime fact, must agree: the default matches it."""
    docs = run()
    for d in docs[1::2]:
        d["coordinates"]["environment"]["system"]["runtime"] = {"python": "3.13"}
    assert invariants(evaluate(docs)) == ["sides-agree"]


def test_exclusions():
    """C6: a failed attempt is listed and its round drops out of the pairing."""
    docs = run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    docs[2]["measurement"] = {"quantity": docs[2]["measurement"]["quantity"], "status": "error",
                              "reason": "harness.error"}
    doc = evaluate(docs)
    result = {"producer": "benchx/gbench-adapter", "ingest_key": docs[2]["ingest_key"]}
    assert doc["exclusions"] == [{"result": result, "reason": "error: harness.error"}]
    assert doc["units"][0]["rounds"] == 3


def test_min_rounds():
    """C9: two rounds are not enough by default."""
    unit = evaluate(run(rounds=2))["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-rounds")
    assert evaluate(run(rounds=2), min_rounds=2)["units"][0]["verdict"] == "regressed"


@pytest.mark.parametrize("min_rounds", [1, 0, -1])
def test_one_pair_has_no_noise_whatever_min_rounds_says(min_rounds):
    """The noise is the standard error of the per-round differences, which needs two
    of them: with one pair the unit is indeterminate, not a StatisticsError."""
    unit = evaluate(run(rounds=1), min_rounds=min_rounds)["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-rounds")
    assert unit["rounds"] == 1 and "noise" not in unit


def test_no_pair_at_all_is_indeterminate_too():
    """Every head attempt failed, so no round pairs; `min_rounds=0` must not reach the mean of nothing."""
    docs = run()
    for d in docs[1::2]:
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
    unit = evaluate(docs, min_rounds=0)["units"][0]
    assert (unit["verdict"], unit["reason"], unit["rounds"]) == ("indeterminate", "insufficient-rounds", 0)


def test_two_pairs_are_enough_when_min_rounds_allows():
    assert evaluate(run(rounds=2), min_rounds=1)["units"][0]["verdict"] == "regressed"


def test_unmatched_differences():
    """C10: a fact the profile ignores that differs between the sides is carried as a caveat."""
    docs = run()
    for d in docs[1::2]:
        d["coordinates"]["environment"]["system"]["os"] = {"kernel": "6.9"}
    assert evaluate(docs)["units"][0]["unmatched_differences"] == ["/coordinates/environment/system/os/kernel"]


def test_condition_measurements_are_not_compared():
    """A load-average sibling (schema §4.4), kind condition, is neither a unit nor an input."""
    docs = run()
    for d in list(docs):
        load = copy.deepcopy(d)
        load["ingest_key"] = d["ingest_key"].replace("wall-time", "load-average")
        load["coordinates"]["protocol"] = {"probe": {"name": "os.getloadavg"}}
        load["measurement"] = {"quantity": {"name": "load-average", "unit": "1"}, "kind": "condition",
                               "status": "success", "observations": [5.0]}
        docs.append(load)
    doc = evaluate(docs)
    assert [u["quantity"] for u in doc["units"]] == ["wall-time"] and len(doc["inputs"]) == 6


def test_a_measurand_read_by_a_probe_is_compared():
    """A measurand is compared however it was read: a probe alone marks nothing."""
    docs = run()
    for d in docs:
        d["coordinates"]["protocol"] = {"probe": {"name": "OSSMemoryProbe", "counter": "rss"}}
        d["measurement"]["quantity"] = {"name": "peak-rss", "unit": "By"}
    assert [u["quantity"] for u in evaluate(docs)["units"]] == ["peak-rss"]


def test_deterministic_quantities_are_deferred():
    docs = run()
    for d in docs:
        d["measurement"]["quantity"]["deterministic"] = True
    unit = evaluate(docs)["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "deterministic-deferred")


def test_environments_needs_its_label():
    docs = copy.deepcopy(run())
    with pytest.raises(compare.CompareError, match="label"):
        compare.compare(docs, run_key="run", profile="environments", baseline="plain")


def labeled(docs, configurations=("plain", "hardened")):
    """The run as an environments run: one tree, sides named by a label and built differently."""
    for d in docs:
        side = 0 if d["revision"]["key"] == BASE else 1
        d["revision"]["key"] = d["provenance"]["subject_tree"] = BASE
        d["provenance"]["labels"] = {"build": ("plain", "hardened")[side]}
        d["coordinates"]["subject"]["configuration"] = {"hardening": configurations[side]}
    return docs


def test_environments_sides_are_named_by_label():
    doc = compare.compare(labeled(run()), run_key="run", profile="environments", baseline="plain", label="build")
    assert core.check(doc, "comparison-document") == []
    assert (doc["varying"], doc["baseline"], doc["contender"]) == ("provenance.labels.build", "plain", "hardened")
    assert doc["failed_invariants"] == [] and doc["units"][0]["verdict"] == "regressed"
    assert doc["units"][0]["unmatched_differences"] == ["/coordinates/subject/configuration/hardening"]


def test_environments_sides_must_differ_beyond_their_label():
    doc = compare.compare(labeled(run(), ("same", "same")), run_key="run", profile="environments",
                          baseline="plain", label="build")
    assert invariants(doc) == ["sides-differ"]


def test_environments_a_label_with_two_builds_is_three_sides():
    """A side is its whole axis value: one label covering two builds does not blend them."""
    docs = labeled(run())
    docs[2]["coordinates"]["subject"]["configuration"] = {"hardening": "other"}
    doc = compare.compare(docs, run_key="run", profile="environments", baseline="plain", label="build")
    assert invariants(doc) == ["two-sides"]


# --- dirty trees and the benchmark file (UC-03 §10, schema §5.5) -------------------------------

OTHER_SUITE = {"uri": "https://example.org/suites.git", "type": "git"}


def dirty(docs, side, flag="subject_dirty"):
    for d in docs:
        if d["revision"]["key"] == side:
            d["provenance"][flag] = "dirty"
    return docs


def benchmark_trees(docs, tree_of, source=OTHER_SUITE):
    """Record the benchmark of every result apart from the subject, as the file `tree_of(doc)`."""
    for d in docs:
        d["provenance"]["benchmark_tree"] = tree_of(d)
        d["benchmark"] = {"source": dict(source)}
    return docs


def test_a_dirty_side_is_compared_and_named():
    doc = evaluate(dirty(run(), HEAD))
    assert invariants(doc) == [] and doc["local_only"] is True
    assert doc["units"][0]["verdict"] == "regressed"
    assert doc["units"][0]["caveats"] == ["contender: dirty-subject-tree"]  # the clean baseline is not named
    shown = compare.render(doc)
    assert "[local-only]" in shown and "caveats: contender: dirty-subject-tree" in shown


def test_both_sides_dirty_are_each_named():
    doc = evaluate(dirty(dirty(run(), HEAD), BASE, "benchmark_dirty"))
    assert invariants(doc) == [] and doc["local_only"] is True
    assert doc["units"][0]["caveats"] == ["baseline: dirty-benchmark-tree", "contender: dirty-subject-tree"]


def test_clean_sides_are_not_local_only():
    doc = evaluate(run())
    assert doc["local_only"] is False and "caveats" not in doc["units"][0]


def test_a_tree_edited_during_the_run_is_a_third_side():
    """A side is a tree id, so a tree that changes mid-run leaves three sides and no verdict."""
    docs = run()
    [d for d in docs if d["revision"]["key"] == HEAD][-1]["provenance"]["subject_tree"] = "e" * 40
    doc = evaluate(docs)
    assert invariants(doc) == ["two-sides"] and doc["units"] == []


def test_a_side_without_a_tree_id_cannot_be_named():
    """An unknown dirty state has no tree id (schema §4.1); the message is about the tree, not about labels."""
    docs = run()
    for d in docs:
        if d["revision"]["key"] == HEAD:
            d["provenance"]["subject_dirty"] = "unknown"
            del d["provenance"]["subject_tree"]
    doc = evaluate(docs)
    assert invariants(doc) == ["sides"] and doc["units"] == []
    detail = doc["failed_invariants"][0]["detail"]
    assert "unknown" in detail and "--label" not in detail


def test_a_benchmark_file_that_is_one_file_passes_whether_clean_or_dirty():
    docs = benchmark_trees(run(), lambda d: "c" * 40)
    assert invariants(evaluate(docs)) == []
    doc = evaluate(dirty(dirty(docs, BASE, "benchmark_dirty"), HEAD, "benchmark_dirty"))
    assert invariants(doc) == [] and doc["local_only"] is True


def test_a_benchmark_file_that_differs_between_the_sides_fails():
    docs = benchmark_trees(run(), lambda d: ("c" if d["revision"]["key"] == BASE else "d") * 40)
    doc = evaluate(docs)
    assert invariants(doc) == ["one-benchmark-tree"] and doc["units"] == []
    assert "2 benchmark trees" in doc["failed_invariants"][0]["detail"]


def test_a_benchmark_file_that_changes_between_rounds_fails():
    docs = benchmark_trees(run(), lambda d: "c" * 40)
    docs[-1]["provenance"]["benchmark_tree"] = "d" * 40  # the suite is edited before the last result
    assert invariants(evaluate(docs)) == ["one-benchmark-tree"]


def test_a_benchmark_in_the_subjects_tree_differs_between_sides_by_design():
    docs = run()
    for d in docs:  # as the runner records an in-tree suite: the subject's tree and the subject's source
        d["provenance"]["benchmark_tree"] = d["provenance"]["subject_tree"]
        d["benchmark"] = {"source": d["source"]}
    assert invariants(evaluate(docs)) == []


def test_an_in_tree_record_that_names_another_source_is_malformed():
    docs = run()
    for d in docs:
        d["provenance"]["benchmark_tree"] = d["provenance"]["subject_tree"]
        d["benchmark"] = {"source": dict(OTHER_SUITE)}
    doc = evaluate(docs)
    assert invariants(doc) == ["one-benchmark-tree"]
    assert doc["failed_invariants"][0]["detail"].startswith("malformed record")


def test_one_side_in_tree_and_one_apart_fails():
    docs = run()
    for d in docs:
        if d["revision"]["key"] == BASE:
            d["provenance"]["benchmark_tree"] = d["provenance"]["subject_tree"]
            d["benchmark"] = {"source": d["source"]}
        else:
            d["provenance"]["benchmark_tree"] = "c" * 40
            d["benchmark"] = {"source": dict(OTHER_SUITE)}
    # the benchmark's source URI differs too, so the identity match (`sides-agree`) fails beside it
    assert "one-benchmark-tree" in invariants(evaluate(docs))


def test_a_suite_in_another_checkout_of_the_same_repository_counts_as_apart():
    """Same canonical source URI as the subject, but its own file id: it is not in-tree."""
    docs = run()
    for d in docs:
        d["provenance"]["benchmark_tree"] = ("c" if d["revision"]["key"] == BASE else "d") * 40
        d["benchmark"] = {"source": dict(d["source"])}
    assert invariants(evaluate(docs)) == ["one-benchmark-tree"]
    same = benchmark_trees(run(), lambda d: "c" * 40, source={"uri": "https://example.org/demo"})
    assert invariants(evaluate(same)) == []  # and one file on both sides passes


def test_a_result_without_a_benchmark_tree_is_outside_the_rule():
    """A thin result has no benchmark_tree: nothing to compare."""
    docs = run()
    assert all("benchmark_tree" not in d["provenance"] for d in docs)
    assert invariants(evaluate(docs)) == []


# --- variants (UC-02, schema §5.5): the sides are two values of one workload parameter ---------

VALUES = ("'baseline'", "'variant'")


def variants_run(rounds=3, sizes=(None,), ratio=1.1, base_values=(1.0, 2.0, 3.0), name="role", values=VALUES):
    """Alternating attempts of the two values of the parameter `name`, one slot each; the second
    value is `ratio` times the first in each round. With `sizes`, a slot holds one result per
    size, and `size` is a second workload parameter. Every result is one revision and one tree."""
    docs, slot = [], 0
    for r in range(rounds):
        for value, scale in zip(values, (1, ratio)):
            for size in sizes:
                parameters = {name: value, **({"size": size} if size is not None else {})}
                docs.append(result(HEAD, r, slot, base_values[r] * scale, ingest_key=f"run:{slot}:{size}",
                                   coordinates__workload={"name": "bench_truncate", "parameters": parameters}))
            slot += 1
    return docs


def vevaluate(docs, parameter="role", baseline="'baseline'", **kwargs):
    doc = compare.compare(docs, run_key="run", profile="variants", baseline=baseline, parameter=parameter, **kwargs)
    assert core.check(doc, "comparison-document") == []
    return doc


def test_variants_a_clean_run_has_one_unit_per_benchmark():
    doc = vevaluate(variants_run())
    assert invariants(doc) == [] and doc["profile"] == "variants"
    assert doc["varying"] == "coordinates.workload.parameters.role"
    assert (doc["baseline"], doc["contender"]) == VALUES
    [unit] = doc["units"]
    assert unit["workload"] == {"name": "bench_truncate"}  # the varied parameter is not part of the unit
    assert unit["verdict"] == "regressed" and unit["effect"] == pytest.approx(0.1)
    assert doc["local_only"] is False


def test_variants_the_baseline_can_be_either_value():
    doc = vevaluate(variants_run(), baseline="'variant'")
    assert invariants(doc) == [] and (doc["baseline"], doc["contender"]) == VALUES[::-1]
    assert doc["units"][0]["verdict"] == "improved"


def test_variants_another_parameter_makes_one_unit_per_value_of_it():
    doc = vevaluate(variants_run(sizes=("1000", "100000")))
    assert invariants(doc) == []
    assert [u["workload"] for u in doc["units"]] == [
        {"name": "bench_truncate", "parameters": {"size": "1000"}},
        {"name": "bench_truncate", "parameters": {"size": "100000"}}]  # no `role` in either
    shown = compare.render(doc)
    assert "bench_truncate [size=1000]" in shown and "bench_truncate [size=100000]" in shown
    assert "role" not in shown.split("\n", 1)[1]


def test_variants_no_parameter_name_is_special():
    doc = vevaluate(variants_run(name="impl"), parameter="impl")
    assert invariants(doc) == [] and doc["varying"] == "coordinates.workload.parameters.impl"


def test_variants_a_third_value_is_a_third_side():
    docs = variants_run()
    docs.append(result(HEAD, 3, 6, 1.0, ingest_key="run:6", coordinates__workload={
        "name": "bench_truncate", "parameters": {"role": "'other'"}}))
    doc = vevaluate(docs)
    assert invariants(doc) == ["two-sides"] and doc["units"] == []


@pytest.mark.parametrize("slotless", [False, True])
def test_variants_a_run_that_does_not_alternate_names_the_ways_to_get_one(slotless):
    """One `bx bench` call runs both values in one order, with no round and no slot."""
    docs = variants_run()
    for d in docs:
        if slotless:
            del d["procedure"]
        else:
            d["procedure"]["slot"] = d["procedure"]["round"]  # both values in one slot
    doc = vevaluate(docs)
    assert invariants(doc) == ["alternating-slots"]
    detail = doc["failed_invariants"][0]["detail"]
    assert "--round and --slot" in detail and "bx compare TARGET --profile variants" in detail


def test_the_other_profiles_do_not_name_variants_in_the_alternation_detail():
    docs = run()
    for d in docs:
        del d["procedure"]
    assert "variants" not in evaluate(docs)["failed_invariants"][0]["detail"]


def test_variants_unequal_attempts():
    docs = variants_run()
    docs.append(result(HEAD, 3, 6, 4.0, ingest_key="run:6", coordinates__workload={
        "name": "bench_truncate", "parameters": {"role": VALUES[0]}}))
    doc = vevaluate(docs)
    assert invariants(doc) == ["equal-attempts"] and doc["units"] == []


def fail(doc, status):
    doc["measurement"] = {"quantity": doc["measurement"]["quantity"], "status": status,
                          **({"reason": "timeout"} if status != "partial" else {"reason": "timeout", "observations": [1.0]})}
    return doc


@pytest.mark.parametrize("status", ["error", "censored", "partial"])
@pytest.mark.parametrize("which", [0, 1])
def test_variants_a_failed_attempt_on_either_side_fails_the_unit(status, which):
    """UC-02 §10: a value that times out in the slow rounds is not judged on the fast ones."""
    docs = variants_run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    failing = docs[2 + which]  # round 1: the baseline's result, then the variant's
    fail(failing, status)
    doc = vevaluate(docs)
    [unit] = doc["units"]
    assert invariants(doc) == [] and unit["verdict"] == "indeterminate" and unit["reason"] == "side-failed"
    assert "effect" not in unit and "noise" not in unit and unit["rounds"] == 3
    [excluded] = doc["exclusions"]
    assert excluded["result"]["ingest_key"] == failing["ingest_key"] and excluded["reason"].startswith(status)
    assert "(side-failed)" in compare.render(doc)


def test_variants_only_the_unit_with_the_failed_attempt_is_indeterminate():
    docs = variants_run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0), sizes=("1000", "100000"))
    fail(docs[4], "error")  # round 1, baseline, size 1000
    verdicts = {u["workload"]["parameters"]["size"]: u["verdict"] for u in vevaluate(docs)["units"]}
    assert verdicts == {"1000": "indeterminate", "100000": "regressed"}


def test_variants_a_skipped_attempt_is_an_ordinary_exclusion():
    docs = variants_run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    docs[2]["measurement"] = {"quantity": docs[2]["measurement"]["quantity"], "status": "skipped", "reason": "n/a"}
    doc = vevaluate(docs)
    [unit] = doc["units"]
    assert unit["verdict"] == "regressed" and unit["rounds"] == 3 and "reason" not in unit
    assert len(doc["exclusions"]) == 1


def test_a_failed_attempt_under_revisions_still_gives_a_verdict_from_the_remaining_pairs():
    docs = run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    fail(docs[2], "error")
    unit = evaluate(docs)["units"][0]
    assert unit["verdict"] == "regressed" and unit["rounds"] == 3 and "reason" not in unit


def test_variants_the_sides_must_share_the_subject_tree():
    docs = variants_run()
    for d in docs:
        if d["coordinates"]["workload"]["parameters"]["role"] == VALUES[1]:
            d["provenance"]["subject_tree"] = "c" * 40
    doc = vevaluate(docs)
    assert invariants(doc) == ["sides-agree"]
    assert doc["failed_invariants"][0]["detail"] == "/provenance/subject_tree differ"


def test_variants_a_suite_that_changes_between_rounds_fails():
    docs = benchmark_trees(variants_run(), lambda d: "c" * 40)
    assert invariants(vevaluate(docs)) == []
    docs[-1]["provenance"]["benchmark_tree"] = "d" * 40  # the suite is edited before the last result
    assert invariants(vevaluate(docs)) == ["one-benchmark-tree"]


def test_variants_a_dirty_tree_is_compared_and_named():
    doc = vevaluate(dirty(variants_run(), HEAD))
    assert invariants(doc) == [] and doc["local_only"] is True
    assert doc["units"][0]["caveats"] == ["baseline: dirty-subject-tree", "contender: dirty-subject-tree"]
    assert "[local-only]" in compare.render(doc)


def test_variants_a_result_without_the_parameter_cannot_be_placed():
    docs = variants_run()
    docs[0]["coordinates"]["workload"]["parameters"] = {"other": "1"}
    doc = vevaluate(docs)
    assert invariants(doc) == ["sides"] and doc["units"] == []
    assert "no workload parameter role" in doc["failed_invariants"][0]["detail"]
    assert "params" in doc["failed_invariants"][0]["detail"]


def test_variants_the_baseline_is_matched_exactly_as_recorded():
    with pytest.raises(compare.CompareError) as error:
        vevaluate(variants_run(), baseline="baseline")  # the recorded value has its quotes
    assert "'baseline'" in str(error.value) and "'variant'" in str(error.value)


@pytest.mark.parametrize("parameter", [None, "", "a b", "a.b", "a/b", "role]"])
def test_variants_needs_a_parameter_that_is_a_token(parameter):
    with pytest.raises(compare.CompareError):
        compare.compare(variants_run(), run_key="run", profile="variants", baseline=VALUES[0], parameter=parameter)


# --- which parameter to vary, in read mode ------------------------------------------------------

def test_infer_parameter_takes_the_only_one():
    assert compare.infer_parameter(variants_run(), "run") == "role"
    assert compare.infer_parameter(variants_run(name="impl"), "run") == "impl"  # no name is special


def test_infer_parameter_refuses_several_and_names_them():
    with pytest.raises(compare.CompareError, match=r"several workload parameters \(role, size\): give --vary NAME"):
        compare.infer_parameter(variants_run(sizes=("1000",)), "run")


def test_infer_parameter_counts_an_environment_variable():
    """The runner records an `-e` variable as a workload parameter of every result."""
    docs = variants_run()
    for d in docs:
        d["coordinates"]["workload"]["parameters"]["OMP_NUM_THREADS"] = "1"
    with pytest.raises(compare.CompareError, match="OMP_NUM_THREADS, role"):
        compare.infer_parameter(docs, "run")


def test_infer_parameter_refuses_a_run_with_no_parameter():
    docs = variants_run()
    for d in docs:
        del d["coordinates"]["workload"]["parameters"]
    with pytest.raises(compare.CompareError, match="nothing to vary"):
        compare.infer_parameter(docs, "run")


def test_infer_parameter_reads_only_the_run_and_only_measurands():
    docs = variants_run()
    other = result(HEAD, 0, 0, 1.0, ingest_key="other", coordinates__workload={
        "name": "x", "parameters": {"size": "1"}}, provenance__run_key="another")
    assert compare.infer_parameter([*docs, other], "run") == "role"
    with pytest.raises(compare.CompareError, match="no results for run"):
        compare.infer_parameter(docs, "missing")
