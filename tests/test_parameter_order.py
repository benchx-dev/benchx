"""Parameters in the order the benchmark declared them (PARAMETER_ORDER_PLAN.md).

The store keeps a document in canonical form, with its keys sorted, so a benchmark's parameters come back
alphabetical: `array_type, dtype, k` for a benchmark that declares `dtype, array_type, k`, which is the order a
filter searches and asv shows. The runner records the declared order in `provenance.info.parameter_names`, and what
shows a workload reads it back.
"""

import json

from benchx import bench, compare, core, tables
from benchx.projection import declared_workload, workload_label
from test_comparator import BASE, HEAD, result

NAME = "bm.Partition.time_argpartition"
DECLARED = ["dtype", "array_type", "k"]  # not alphabetical
COMBINATIONS = [
    {"dtype": dtype, "array_type": array_type, "k": "10"}
    for dtype in ("'float64'", "'int16'")
    for array_type in ("('ordered',)", "('random',)")
]


def stored(document):
    """A document as the store gives it back: canonical, so its keys sorted."""
    return json.loads(core.canonical(document))


def case(side, round_, slot, value, parameters, names=True):
    d = result(side, round_, slot, value, workload=NAME)
    d["coordinates"]["workload"]["parameters"] = parameters  # in the order the benchmark declared them
    d["ingest_key"] += f":{parameters}"
    if names:
        d["provenance"]["info"] = {"parameter_names": list(parameters)}
    return stored(d)


def run_of(names=True, rounds=5, ratio=1.5):
    """Every combination, base and head alternating; the head is `ratio` times the base."""
    docs, slot = [], 0
    for r in range(rounds):
        for side in (BASE, HEAD):
            for parameters in COMBINATIONS:
                docs.append(case(side, r, slot, (1.0 + 0.01 * r) * (ratio if side == HEAD else 1.0), parameters, names))
            slot += 1
    return docs


def evaluate(docs):
    return compare.compare(docs, run_key="run", profile="revisions", baseline=BASE)


def test_the_store_gives_the_parameters_back_alphabetical():
    """The fault: what a result file says in one order, the stored document says in another."""
    document = case(BASE, 0, 0, 1.0, COMBINATIONS[0])
    assert list(document["coordinates"]["workload"]["parameters"]) == sorted(DECLARED)
    assert document["provenance"]["info"]["parameter_names"] == DECLARED  # which is why the order is recorded


def test_a_workload_is_read_in_the_order_its_result_recorded():
    document = case(BASE, 0, 0, 1.0, COMBINATIONS[0])
    assert list(declared_workload(document)["parameters"]) == DECLARED
    assert declared_workload(document)["name"] == NAME
    assert list(document["coordinates"]["workload"]["parameters"]) == sorted(DECLARED)  # the document is not changed


def test_a_result_that_recorded_no_order_has_its_parameters_as_stored():
    """Made before the order was recorded, or by another producer."""
    document = case(BASE, 0, 0, 1.0, COMBINATIONS[0], names=False)
    assert declared_workload(document) == document["coordinates"]["workload"]
    assert declared_workload(result(BASE, 0, 0, 1.0)) == {"name": "BM_W"}  # nor parameters


def test_a_recorded_order_that_does_not_fit_the_parameters_is_not_trusted_blindly():
    document = case(BASE, 0, 0, 1.0, COMBINATIONS[0])
    document["provenance"]["info"]["parameter_names"] = ["k", "gone"]  # one is not a parameter, two are not named
    assert list(declared_workload(document)["parameters"]) == ["k", "array_type", "dtype"]


def test_a_comparison_names_the_parameters_in_the_order_they_were_declared():
    doc = evaluate(run_of())
    assert [list(u["workload"]["parameters"]) for u in doc["units"]] == [DECLARED] * 4
    assert compare.render(doc).count("[dtype='int16', array_type=('ordered',), k=10]") == 1


def test_the_units_are_listed_in_the_order_the_parameters_vary_as_declared():
    """The first declared parameter changes slowest, as asv lists a product of params."""
    doc = evaluate(run_of())
    assert [
        (u["workload"]["parameters"]["dtype"], u["workload"]["parameters"]["array_type"]) for u in doc["units"]
    ] == [
        ("'float64'", "('ordered',)"),
        ("'float64'", "('random',)"),
        ("'int16'", "('ordered',)"),
        ("'int16'", "('random',)"),
    ]


def test_results_without_the_order_are_compared_and_listed_as_before():
    doc = evaluate(run_of(names=False))
    assert [list(u["workload"]["parameters"]) for u in doc["units"]] == [sorted(DECLARED)] * 4
    assert [
        (u["workload"]["parameters"]["array_type"], u["workload"]["parameters"]["dtype"]) for u in doc["units"]
    ] == [
        ("('ordered',)", "'float64'"),
        ("('ordered',)", "'int16'"),
        ("('random',)", "'float64'"),
        ("('random',)", "'int16'"),
    ]


def test_a_failed_invariant_names_the_parameters_in_the_declared_order_too():
    docs = run_of()
    for d in docs:
        if d["coordinates"]["workload"]["parameters"]["dtype"] == "'int16'":
            d["coordinates"]["environment"]["system"]["runtime"] = {
                "python": "3.13" if d["revision"]["key"] == HEAD else "3.12"
            }
    doc = evaluate(docs)
    assert [list(f["workload"]["parameters"]) for f in doc["failed_invariants"]] == [DECLARED] * 2
    assert compare.render(doc).splitlines()[-1] == (
        "  sides-agree: /coordinates/environment/system/runtime/python differ  (2 units: "
        f"{NAME} [dtype='int16', array_type=('ordered',), k=10], {NAME} [dtype='int16', array_type=('random',), k=10])"
    )


def test_the_tables_have_the_columns_and_the_values_in_the_declared_order():
    docs = run_of()
    text = tables.render(evaluate(docs), docs)
    header = next(line for line in text.splitlines() if "array_type" in line)
    assert header.split() == DECLARED
    assert f"{NAME}('float64', ('ordered',), 10)" in text  # the label of the change table: values, in that order


def test_bx_bench_and_bx_head_label_a_stored_result_in_the_declared_order():
    document = case(BASE, 0, 0, 1.0, COMBINATIONS[3])
    assert bench.row_of(document)["case"] == f"{NAME} [dtype='int16', array_type=('random',), k=10]"
    assert workload_label(declared_workload(document)) == bench.row_of(document)["case"]
