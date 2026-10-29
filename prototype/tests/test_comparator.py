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
