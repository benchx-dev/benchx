"""benchx_params: asv's parameter conventions, shared by the adapters whose benchmarks take them."""

from pathlib import Path

from benchx.adapters import asv_runtime, base, benchx_params as params


def test_stable_repr_drops_a_default_address_and_nothing_else():
    class Plain:
        pass

    class Own:
        def __repr__(self):
            return "<Own at 0x1234>"  # not the default repr, so it is the benchmark's own text

    assert "0x" not in params.stable_repr(Plain())
    assert params.stable_repr(Plain()).endswith("Plain object>")
    assert params.stable_repr(Own()) == "<Own at 0x1234>"
    assert [params.stable_repr(v) for v in (10, "a", None, (1, 2))] == ["10", "'a'", "None", "(1, 2)"]


def test_normalize_reads_a_flat_list_or_one_list_per_parameter():
    assert params.normalize([1, 2], ["n"]) == ([[1, 2]], ["n"], [["1", "2"]])
    assert params.normalize(([10, 100], ["a", "b"]), ["n", "key"]) == (
        [[10, 100], ["a", "b"]], ["n", "key"], [["10", "100"], ["'a'", "'b'"]])
    assert params.normalize([], []) == ([], [], [])


def test_normalize_names_the_parameters_that_have_no_name():
    assert params.normalize([[1], [2]], [])[1] == ["param1", "param2"]
    assert params.normalize([[1], [2]], ["n"])[1] == ["n", "param2"]
    assert params.normalize([1], ["n", "unused"])[1] == ["n"]


def test_normalize_tells_equal_values_apart():
    assert params.normalize([[1, 1, 2, 1]], [])[2] == [["1 (0)", "1 (1)", "2", "1 (2)"]]


def test_combinations_are_in_product_order():
    values = [[1, 2], ["a", "b"]]
    assert [params.combination(values, i) for i in range(4)] == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]


def test_the_runtime_loads_the_module_beside_it():
    assert Path(asv_runtime.params_.__file__) == Path(asv_runtime.__file__).with_name("benchx_params.py")


IDS = [["10", "100"], ["'a'", "'b'"]]  # what `normalize` gives for n in (10, 100) and key in ("a", "b")


def test_a_benchmark_without_parameters_is_one_case_named_for_it():
    assert params.expand("m.time_x", [], None) == [("m.time_x", None)]
    assert params.expand("m.time_x", [], r"time_x$") == [("m.time_x", None)]
    assert params.expand("m.time_x", [], "time_y") == []


def test_a_parametrized_benchmark_is_one_case_per_combination():
    assert params.expand("m.C.time_x", IDS, None) == [
        ("m.C.time_x-0", 0), ("m.C.time_x-1", 1), ("m.C.time_x-2", 2), ("m.C.time_x-3", 3)]


def test_a_filter_is_searched_in_the_name_with_its_arguments():
    # `asv run --bench time_x` runs every combination; `time_x$` and a typo run none.
    assert [i for _, i in params.expand("m.C.time_x", IDS, "time_x")] == [0, 1, 2, 3]
    assert [i for _, i in params.expand("m.C.time_x", IDS, r"^m\.C\.time_x\(100, 'a'\)$")] == [2]
    assert [i for _, i in params.expand("m.C.time_x", IDS, r"\(100,")] == [2, 3]
    assert params.expand("m.C.time_x", IDS, "time_x$") == []
    assert params.expand("m.C.time_x", IDS, "time_y") == []


def test_a_filter_that_matches_only_the_name_is_noticed():
    assert params.matches_name_only("m.C.time_x", IDS, "time_x$")
    assert not params.matches_name_only("m.C.time_x", IDS, "time_y")
    assert not params.matches_name_only("m.time_x", [], "time_x$")  # nothing to put after the name
    assert not params.matches_name_only("m.C.time_x", IDS, None)


def test_the_workload_of_a_case_comes_from_the_table_not_from_the_id():
    runnable = {"cases": {"m.C.time_x-2": ("m.C.time_x", 2), "m.time_y-1": ("m.time_y", None)},
                "benchmarks": {"m.C.time_x": {"params": IDS, "param_names": ["n", "key"]}, "m.time_y": {}}}
    assert base.parametrized_workload("m.C.time_x-2", runnable) == ("m.C.time_x", {"n": "100", "key": "'a'"})
    assert base.parametrized_workload("m.time_y-1", runnable) == ("m.time_y", {})  # a name that looks like -1


def test_the_note_names_the_benchmarks_it_means():
    note = base.name_only_note(["a", "b", "c", "d"])
    assert "(a, b, c)" in note  # three at most
    assert "name(p0, p1)" in note
