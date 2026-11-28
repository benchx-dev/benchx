"""benchx_params: asv's parameter conventions, shared by the adapters whose benchmarks take them."""

import re
from pathlib import Path

from benchx.adapters import asv_runtime, base
from benchx.adapters import benchx_params as params


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
        [[10, 100], ["a", "b"]],
        ["n", "key"],
        [["10", "100"], ["'a'", "'b'"]],
    )
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
        ("m.C.time_x-0", 0),
        ("m.C.time_x-1", 1),
        ("m.C.time_x-2", 2),
        ("m.C.time_x-3", 3),
    ]


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


def test_a_filter_that_gives_the_arguments_in_the_wrong_order_is_noticed():
    """The part before the first `\\(` is the benchmark's name; what follows it selected nothing."""
    assert params.expand("m.C.time_x", IDS, r"C\.time_x\('a', 10\)") == []
    assert params.matches_name_only("m.C.time_x", IDS, r"C\.time_x\('a', 10\)")
    assert params.matches_name_only("m.C.time_x", IDS, r"^m\.C\.time_x\(100, 'c'\)$")
    assert not params.matches_name_only("m.C.time_x", IDS, r"C\.time_y\(10, 'a'\)")  # another benchmark
    assert not params.matches_name_only("m.C.time_x", IDS, r"\(10, ")  # no name to go by
    assert not params.matches_name_only("m.C.time_x", IDS, r"^\(10, ")
    assert not params.matches_name_only("m.C.time_x", IDS, r"(unbalanced\(10")  # not a regex: no hint, no error


def test_the_workload_of_a_case_comes_from_the_table_not_from_the_id():
    runnable = {
        "cases": {"m.C.time_x-2": ("m.C.time_x", 2), "m.time_y-1": ("m.time_y", None)},
        "benchmarks": {"m.C.time_x": {"params": IDS, "param_names": ["n", "key"]}, "m.time_y": {}},
    }
    assert base.parametrized_workload("m.C.time_x-2", runnable) == ("m.C.time_x", {"n": "100", "key": "'a'"})
    assert base.parametrized_workload("m.time_y-1", runnable) == ("m.time_y", {})  # a name that looks like -1


NAMES = [
    "bench_function_base.Partition.time_argpartition",
    "bench_function_base.Partition.time_partition",
    "bench_function_base.Sort.time_sort",
    "bench_linalg.Linalg.time_norm",
    "bench_ufunc.UFunc.time_add",
]


def test_a_slip_in_a_word_of_the_filter_finds_the_names_that_have_it():
    assert base.similar("Partiton", NAMES) == NAMES[:2]  # a class, as spelled in the names
    assert (
        base.similar("time_argpartiton", NAMES) == NAMES[:2]
    )  # a method: its own first (0.97), then time_partition (0.87)
    assert base.similar("bench_linalgg", NAMES) == [NAMES[3]]  # a module
    assert base.similar("partition", NAMES) == NAMES[:2]  # the case of a letter is no difference


def test_the_words_of_a_regex_are_what_is_compared():
    assert base.similar(r"^bench_function_base\.Partiton\.time_x\(10, 'a'\)$", NAMES) == NAMES[:2]
    assert base.similar(r"Partiton|time_ad\b", NAMES) == [NAMES[0], NAMES[1], NAMES[4]]  # 0.94, 0.94, then 0.93
    assert base.similar(r"\d+\.\w", NAMES) == []  # escapes and one letter say nothing


def test_nothing_is_similar_to_what_is_not_like_any_name():
    assert base.similar("zzzz", NAMES) == []
    assert base.similar(None, NAMES) == []
    assert base.similar("Partiton", []) == []


def test_the_most_similar_names_come_first_and_at_most_limit_are_returned():
    names = ["m.Alpha.time_a", "m.Alphb.time_a", "m.Alpxx.time_a", "m.Alpha.time_b"]
    assert base.similar("Alpha", names) == ["m.Alpha.time_a", "m.Alpha.time_b", "m.Alphb.time_a"]  # exact, then near
    assert base.similar("Alpha", names, limit=1) == ["m.Alpha.time_a"]


def test_the_note_names_the_benchmarks_it_means():
    found = [{"name": name, "param_names": ["n", "key"], "params": IDS} for name in "abcd"]
    note = base.name_only_note(found)
    assert "(a, b, c)" in note  # three at most
    assert "name(p0, p1)" in note
    # the first one's parameters, in order, and a case of it, each on a line of its own to be copied
    assert note.splitlines()[2:] == ["  a(n, key)", "for example:", "  a(10, 'a')"]


def test_search_name_is_exactly_what_expand_searches():
    """The session picks a case by this text, and the runner expands its filter against the same text."""
    one = [["'a'", "'b'"]]
    two = [["'a'", "'b'"], ["1", "2"]]
    dupes = [["1 (0)", "1 (1)"], ["'x'"]]
    assert params.search_name("f", one, 1) == "f('b')"
    assert params.search_name("f", two, 3) == "f('b', 2)"
    assert params.search_name("f", dupes, 1) == "f(1 (1), 'x')"
    assert params.search_name("f", [], None) == "f"  # a benchmark without parameters
    for ids in (one, two, dupes):
        for case, index in params.expand("f", ids, None):
            exactly = "^" + re.escape(params.search_name("f", ids, index)) + "$"
            assert params.expand("f", ids, exactly) == [(case, index)]
    assert params.expand("g", [], "^" + re.escape(params.search_name("g", [], None)) + "$") == [("g", None)]
