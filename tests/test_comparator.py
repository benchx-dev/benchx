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
            "subject": {
                "name": "demo",
                "components": [{"role": "primary"}],
                "configuration": {"build_type": "Release"},
            },
            "protocol": {"harness": {"name": "google-benchmark"}},
            "environment": {
                "schema_version": "machine/v1",
                "identity": {"runner": "host"},
                "system": {"os": {"kernel": "6.8"}},
            },
        },
        "measurement": {
            "quantity": {"name": "wall-time", "unit": "s", "direction": "lower-is-better"},
            "status": "success",
            "observations": [value],
        },
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


# The fixtures here have 3 rounds, one fewer than the default min_rounds, so the helpers ask for 3.
FEW = {"min_rounds": 3}


def evaluate(docs, **kwargs):
    doc = compare.compare(docs, run_key="run", profile="revisions", baseline=BASE, **{**FEW, **kwargs})
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
    assert unit["effect"]["value"] == pytest.approx(0.1) and unit["details"]["noise"] == pytest.approx(0, abs=1e-12)


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
    docs[2]["measurement"] = {
        "quantity": docs[2]["measurement"]["quantity"],
        "status": "error",
        "reason": "harness.error",
    }
    doc = evaluate(docs)
    result = {"producer": "benchx/gbench-adapter", "ingest_key": docs[2]["ingest_key"]}
    assert doc["exclusions"] == [{"result": result, "reason": "error: harness.error"}]
    assert doc["units"][0]["details"]["rounds"] == 3


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
    assert unit["details"]["rounds"] == 1 and "noise" not in unit["details"]


def test_no_pair_at_all_is_indeterminate_too():
    """Every head attempt failed, so no round pairs, and the reason says the head was not measured; `min_rounds=0` must not reach the mean of nothing."""
    docs = run()
    for d in docs[1::2]:
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
    unit = evaluate(docs, min_rounds=0)["units"][0]
    assert (unit["verdict"], unit["reason"], unit["details"]["rounds"]) == ("indeterminate", "not-measured", 0)


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
        load["measurement"] = {
            "quantity": {"name": "load-average", "unit": "1"},
            "kind": "condition",
            "status": "success",
            "observations": [5.0],
        }
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
    doc = compare.compare(labeled(run()), run_key="run", profile="environments", baseline="plain", label="build", **FEW)
    assert core.check(doc, "comparison-document") == []
    assert (doc["sides"]["varying"], doc["sides"]["baseline"], doc["sides"]["contender"]) == (
        "provenance.labels.build",
        "plain",
        "hardened",
    )
    assert doc["failed_invariants"] == [] and doc["units"][0]["verdict"] == "regressed"
    assert doc["units"][0]["unmatched_differences"] == ["/coordinates/subject/configuration/hardening"]


def test_environments_sides_must_differ_beyond_their_label():
    doc = compare.compare(
        labeled(run(), ("same", "same")), run_key="run", profile="environments", baseline="plain", label="build"
    )
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
                docs.append(
                    result(
                        HEAD,
                        r,
                        slot,
                        base_values[r] * scale,
                        ingest_key=f"run:{slot}:{size}",
                        coordinates__workload={"name": "bench_truncate", "parameters": parameters},
                    )
                )
            slot += 1
    return docs


def vevaluate(docs, parameter="role", baseline="'baseline'", **kwargs):
    doc = compare.compare(
        docs, run_key="run", profile="variants", baseline=baseline, parameter=parameter, **{**FEW, **kwargs}
    )
    assert core.check(doc, "comparison-document") == []
    return doc


def test_variants_a_clean_run_has_one_unit_per_benchmark():
    doc = vevaluate(variants_run())
    assert invariants(doc) == [] and doc["selector"]["params"]["profile"] == "variants"
    assert doc["sides"]["varying"] == "coordinates.workload.parameters.role"
    assert (doc["sides"]["baseline"], doc["sides"]["contender"]) == VALUES
    [unit] = doc["units"]
    assert unit["workload"] == {"name": "bench_truncate"}  # the varied parameter is not part of the unit
    assert unit["verdict"] == "regressed" and unit["effect"]["value"] == pytest.approx(0.1)
    assert doc["local_only"] is False


def test_variants_the_baseline_can_be_either_value():
    doc = vevaluate(variants_run(), baseline="'variant'")
    assert invariants(doc) == [] and (doc["sides"]["baseline"], doc["sides"]["contender"]) == VALUES[::-1]
    assert doc["units"][0]["verdict"] == "improved"


def test_variants_another_parameter_makes_one_unit_per_value_of_it():
    doc = vevaluate(variants_run(sizes=("1000", "100000")))
    assert invariants(doc) == []
    assert [u["workload"] for u in doc["units"]] == [
        {"name": "bench_truncate", "parameters": {"size": "1000"}},
        {"name": "bench_truncate", "parameters": {"size": "100000"}},
    ]  # no `role` in either
    shown = compare.render(doc)
    assert "bench_truncate [size=1000]" in shown and "bench_truncate [size=100000]" in shown
    assert "role" not in shown.split("\n", 1)[1]


def test_variants_no_parameter_name_is_special():
    doc = vevaluate(variants_run(name="impl"), parameter="impl")
    assert invariants(doc) == [] and doc["sides"]["varying"] == "coordinates.workload.parameters.impl"


def test_variants_a_third_value_is_a_third_side():
    docs = variants_run()
    docs.append(
        result(
            HEAD,
            3,
            6,
            1.0,
            ingest_key="run:6",
            coordinates__workload={"name": "bench_truncate", "parameters": {"role": "'other'"}},
        )
    )
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
    docs.append(
        result(
            HEAD,
            3,
            6,
            4.0,
            ingest_key="run:6",
            coordinates__workload={"name": "bench_truncate", "parameters": {"role": VALUES[0]}},
        )
    )
    doc = vevaluate(docs)
    assert invariants(doc) == ["equal-attempts"] and doc["units"] == []


def fail(doc, status):
    doc["measurement"] = {
        "quantity": doc["measurement"]["quantity"],
        "status": status,
        **({"reason": "timeout"} if status != "partial" else {"reason": "timeout", "observations": [1.0]}),
    }
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
    assert "effect" not in unit and "noise" not in unit["details"] and unit["details"]["rounds"] == 3
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
    assert unit["verdict"] == "regressed" and unit["details"]["rounds"] == 3 and "reason" not in unit
    assert len(doc["exclusions"]) == 1


def test_a_failed_attempt_under_revisions_still_gives_a_verdict_from_the_remaining_pairs():
    docs = run(rounds=4, base_values=(1.0, 2.0, 3.0, 4.0))
    fail(docs[2], "error")
    unit = evaluate(docs)["units"][0]
    assert unit["verdict"] == "regressed" and unit["details"]["rounds"] == 3 and "reason" not in unit


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
    other = result(
        HEAD,
        0,
        0,
        1.0,
        ingest_key="other",
        coordinates__workload={"name": "x", "parameters": {"size": "1"}},
        provenance__run_key="another",
    )
    assert compare.infer_parameter([*docs, other], "run") == "role"
    with pytest.raises(compare.CompareError, match="no results for run"):
        compare.infer_parameter(docs, "missing")


# --- units are listed in natural order ----------------------------------------------------------


def test_units_are_listed_with_numbers_in_natural_order():
    """`n=50` before `n=100`, as people read sizes, though "100" sorts before "50" as text."""
    doc = vevaluate(variants_run(sizes=("100", "10", "50", "1000")))
    assert invariants(doc) == []
    assert [u["workload"]["parameters"]["size"] for u in doc["units"]] == ["10", "50", "100", "1000"]
    shown = [line.split()[1] for line in compare.render(doc).splitlines() if "bench_truncate [" in line]
    assert shown == ["[size=10]", "[size=50]", "[size=100]", "[size=1000]"]


def test_units_are_listed_with_numbers_in_workload_names_in_natural_order():
    docs = []
    for name in ("BM_W10", "BM_W2", "BM_W1"):
        slot = 0
        for r in range(3):
            for side, scale in ((BASE, 1.0), (HEAD, 1.1)):
                docs.append(result(side, r, slot, (r + 1) * scale, workload=name))
                slot += 1
    assert [u["workload"]["name"] for u in evaluate(docs)["units"]] == ["BM_W1", "BM_W2", "BM_W10"]


def test_units_with_more_than_one_other_parameter_are_ordered_by_name_then_value():
    docs = variants_run()
    extra = []
    for n in ("10", "2"):
        for size in ("100", "20"):
            for d in docs:
                clone = copy.deepcopy(d)
                clone["ingest_key"] += f":{n}:{size}"
                clone["coordinates"]["workload"]["parameters"].update(n=n, size=size)
                extra.append(clone)
    doc = vevaluate(extra)
    assert [(u["workload"]["parameters"]["n"], u["workload"]["parameters"]["size"]) for u in doc["units"]] == [
        ("2", "20"),
        ("2", "100"),
        ("10", "20"),
        ("10", "100"),
    ]


# --- one benchmark that cannot be compared does not stop the others -------------------------


def run_two(rounds=3, ratio=1.1):
    """Two workloads measured in every call; the head takes `ratio` times the base's time."""
    docs, slot = [], 0
    for r in range(rounds):
        for side, factor in ((BASE, 1.0), (HEAD, ratio)):
            docs += [result(side, r, slot, (r + 1.0) * factor, workload=name) for name in ("BM_A", "BM_B")]
            slot += 1
    return docs


def of_head(docs, workload):
    return [d for d in docs if d["revision"]["key"] == HEAD and d["coordinates"]["workload"]["name"] == workload]


def by_name(doc):
    return {u["workload"]["name"]: u for u in doc["units"]}


def test_an_edited_benchmark_is_not_comparable_and_the_others_are_compared():
    """A benchmark whose code differs between the sides (asv's benchmark version) is a new series: as in
    `asv compare` it is "not comparable", is no failed invariant, and the other benchmark is compared."""
    docs = run_two()
    for d in docs:
        if d["coordinates"]["workload"]["name"] == "BM_B":
            version = "v2" if d["revision"]["key"] == HEAD else "v1"
            d["coordinates"]["protocol"]["asv"] = {"benchmark_version": version}
    doc = evaluate(docs)
    assert invariants(doc) == []
    edited = by_name(doc)["BM_B"]
    assert (edited["verdict"], edited["reason"], edited["details"]["rounds"]) == (
        "indeterminate",
        "benchmark-changed",
        0,
    )
    assert edited["attempts"] == {"baseline": {"success": 3}, "contender": {"success": 3}}
    assert "effect" not in edited
    assert by_name(doc)["BM_A"]["verdict"] == "regressed"
    text = compare.render(doc)
    assert "\nNOT COMPARABLE (1)\n  BM_B" in text and "(benchmark-changed: its version differs" in text
    assert "INDETERMINATE" not in text


def test_a_benchmark_edited_during_the_run_is_not_comparable_either():
    """The version differs between the rounds of one side, not only between the sides."""
    docs = run_two()
    for d in docs:
        if d["coordinates"]["workload"]["name"] == "BM_B":
            d["coordinates"]["protocol"]["asv"] = {"benchmark_version": "v2" if d["procedure"]["round"] else "v1"}
    doc = evaluate(docs)
    assert invariants(doc) == [] and by_name(doc)["BM_B"]["reason"] == "benchmark-changed"


def test_a_version_and_another_fact_that_differ_together_are_still_a_failed_invariant():
    """Only the benchmark's own version is "not comparable": a runtime that differs as well is the sides
    disagreeing about more than the benchmark."""
    docs = run_two()
    for d in docs:
        if d["coordinates"]["workload"]["name"] == "BM_B":
            head = d["revision"]["key"] == HEAD
            d["coordinates"]["protocol"]["asv"] = {"benchmark_version": "v2" if head else "v1"}
            d["coordinates"]["environment"]["system"]["runtime"] = {"python": "3.13" if head else "3.12"}
    doc = evaluate(docs)
    assert invariants(doc) == ["sides-agree"] and list(by_name(doc)) == ["BM_A"]


def test_a_failed_attempt_is_excluded_before_the_facts_are_matched():
    """A benchmark that raises reports no harness version or runtime, so its attempts differ from the other
    side's in those facts. They are excluded and listed, not matched: the benchmark has no verdict
    (`not-measured`) and the other one is compared."""
    docs = run_two()
    failing = of_head(docs, "BM_B")
    for d in docs:
        if d not in failing:  # the CPU configuration is a fact the projection ignores: what is reported, differs
            d["coordinates"]["environment"]["system"]["cpu"] = {"config": "performance"}
    for d in failing:
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
        d["coordinates"]["protocol"]["harness"] = {"name": "google-benchmark", "version": "1.9"}
        d["coordinates"]["environment"]["system"]["runtime"] = {"python": "3.12"}
    doc = evaluate(docs)
    assert doc["failed_invariants"] == []
    assert by_name(doc)["BM_A"]["verdict"] == "regressed"
    failed = by_name(doc)["BM_B"]
    assert failed["verdict"] == "indeterminate" and failed["reason"] == "not-measured"
    assert [e["reason"] for e in doc["exclusions"]] == ["error: harness.error"] * 3
    assert by_name(doc)["BM_B"].get("unmatched_differences", []) == []  # what it did not report is no difference
    assert by_name(doc)["BM_A"].get("unmatched_differences", []) == []


def test_a_failure_of_the_whole_run_still_leaves_no_units():
    """A third side (a tree edited during the run) is about the run, so no benchmark is compared."""
    docs = run_two()
    docs[-1]["provenance"]["subject_tree"] = "c" * 40
    doc = evaluate(docs)
    assert invariants(doc) == ["two-sides"] and doc["units"] == []


# --- what the output says about what was left out (FAILURE_MESSAGES_PLAN.md, decisions 8 and 9) ---


def failing_head(docs, workload="BM_B", message="ValueError: array has no axis 3"):
    """The head's attempts of a workload fail, the worker having said `message`."""
    for d in of_head(docs, workload):
        d["measurement"] = {"quantity": d["measurement"]["quantity"], "status": "error", "reason": "harness.error"}
        if message:
            d["provenance"] = {**d["provenance"], "info": {"message": message}}
    return docs


def test_an_exclusion_says_what_the_failed_attempt_said():
    doc = evaluate(failing_head(run_two()))
    assert [e["reason"] for e in doc["exclusions"]] == ["error: harness.error: ValueError: array has no axis 3"] * 3


def test_an_exclusion_of_an_attempt_that_said_nothing_is_as_it_was():
    doc = evaluate(failing_head(run_two(), message=None))
    assert [e["reason"] for e in doc["exclusions"]] == ["error: harness.error"] * 3


def test_excluded_results_that_failed_alike_are_one_line_with_their_number():
    text = compare.render(evaluate(failing_head(run_two())))
    assert text.endswith("\nexcluded (3)\n  error: harness.error: ValueError: array has no axis 3  (3 results)")
    assert "ingest_key" not in text and "{'" not in text  # words, not the document's dicts


def test_excluded_results_that_failed_differently_are_lines_of_their_own():
    docs = failing_head(run_two(), message="ValueError: array has no axis 3")
    docs[docs.index(of_head(docs, "BM_B")[0])]["provenance"]["info"]["message"] = "KeyError: 'x'"
    lines = compare.render(evaluate(docs)).splitlines()
    assert lines[-3:] == [
        "excluded (3)",
        "  error: harness.error: KeyError: 'x'  (1 result)",
        "  error: harness.error: ValueError: array has no axis 3  (2 results)",
    ]


def test_a_message_in_the_reason_is_one_line_and_cut():
    docs = failing_head(run_two(), message="RuntimeError: " + "x" * 300 + "\nthe second line")
    [reason, *_] = {e["reason"] for e in evaluate(docs)["exclusions"]}
    assert reason.startswith("error: harness.error: RuntimeError: xxx") and reason.endswith("…")
    assert "second line" not in reason and len(reason) == len("error: harness.error: ") + 160


def test_a_failed_invariant_of_one_unit_names_the_unit_in_words():
    docs = run_two()
    for d in docs:
        if d["coordinates"]["workload"]["name"] == "BM_B":
            d["coordinates"]["protocol"]["harness"] = {
                "name": "google-benchmark",
                "version": "1.9" if d["revision"]["key"] == HEAD else "1.8",
            }
    text = compare.render(evaluate(docs))
    assert text.endswith("\nfailed invariants (1)\n  BM_B: sides-agree: /coordinates/protocol/harness/version differ")
    assert "'invariant'" not in text


def test_units_that_failed_an_invariant_alike_are_one_line():
    docs = run_two()
    for d in docs:
        d["coordinates"]["protocol"]["harness"] = {
            "name": "google-benchmark",
            "version": "1.9" if d["revision"]["key"] == HEAD else "1.8",
        }
    text = compare.render(evaluate(docs))
    assert text.endswith(
        "\nfailed invariants (2)\n  sides-agree: /coordinates/protocol/harness/version differ  (2 units: BM_A, BM_B)"
    )


def rendered_doc():
    """The least of a comparison document that `render` reads, with nothing in its lists."""
    return {
        "selector": {
            "name": "benchx/run-profile",
            "version": "v1",
            "params": {"run_key": "r", "profile": "revisions", "baseline": BASE},
        },
        "sides": {"varying": "provenance.subject_tree", "baseline": BASE, "contender": HEAD},
        "local_only": False,
        "units": [],
        "missing": [],
        "failed_invariants": [],
        "exclusions": [],
    }


def test_a_failed_invariant_of_the_run_is_in_words_too():
    doc = {
        **rendered_doc(),
        "failed_invariants": [{"invariant": "equal-attempts", "detail": "3 against 2: run the same number of rounds"}],
    }
    assert compare.render(doc).endswith(
        "\nfailed invariants (1)\n  equal-attempts: 3 against 2: run the same number of rounds"
    )


def test_missing_results_are_one_line_for_each_side_with_a_few_names():
    def where(name, side):
        return {"workload": {"name": name, "parameters": {"n": "1"}}, "quantity": "wall-time", "side": side}

    doc = {
        **rendered_doc(),
        "missing": [
            where("a", "contender"),
            where("b", "contender"),
            where("c", "contender"),
            where("d", "contender"),
            where("e", "baseline"),
        ],
    }
    assert compare.render(doc).endswith(
        "\nmissing (5)\n  not on baseline: e [n=1]\n  not on contender: a [n=1], b [n=1], c [n=1], … (+1 more)"
    )


def skip(docs, indexes, status="skipped", reason="harness.skipped"):
    for i in indexes:
        docs[i]["measurement"] = {"quantity": docs[i]["measurement"]["quantity"], "status": status, "reason": reason}
    return docs


def test_a_side_that_skipped_every_round_was_not_measured_not_short_of_rounds():
    """A setup that raises on the contender leaves no pair at all. More rounds would not help, so the
    reason is not `insufficient-rounds`; the unit carries how each side's attempts ended."""
    doc = evaluate(skip(run(), [1, 3, 5]))
    unit = doc["units"][0]
    assert (unit["verdict"], unit["reason"], unit["details"]["rounds"]) == ("indeterminate", "not-measured", 0)
    assert unit["attempts"] == {"baseline": {"success": 3}, "contender": {"skipped": 3}}
    assert "reference_estimate" not in unit["details"] and "effect" not in unit
    assert "rounds=0  (not-measured: contender skipped)" in compare.render(doc)
    assert [e["reason"] for e in doc["exclusions"]] == ["skipped: harness.skipped"] * 3


def test_the_render_names_every_side_that_was_not_measured_and_how_its_attempts_ended():
    docs = skip(skip(run(), [0, 2, 4], "error", "harness.error"), [1, 3], "skipped")
    docs = skip(docs, [5], "error", "harness.error")
    doc = evaluate(docs)
    unit = doc["units"][0]
    assert unit["reason"] == "not-measured"
    assert unit["attempts"] == {"baseline": {"error": 3}, "contender": {"error": 1, "skipped": 2}}
    assert "(not-measured: baseline error, contender skipped 2, error 1)" in compare.render(doc)


def test_some_rounds_measured_on_both_sides_is_still_insufficient_rounds():
    """One skipped round leaves two pairs: the sides were measured, there are too few rounds."""
    unit = evaluate(skip(run(), [5]))["units"][0]
    assert (unit["reason"], unit["details"]["rounds"]) == ("insufficient-rounds", 2)
    assert unit["attempts"] == {"baseline": {"success": 3}, "contender": {"skipped": 1, "success": 2}}
    assert "(insufficient-rounds)" in compare.render(evaluate(skip(run(), [5])))


def test_every_unit_carries_how_its_attempts_ended():
    unit = evaluate(run())["units"][0]
    assert unit["attempts"] == {"baseline": {"success": 3}, "contender": {"success": 3}}
