"""benchx_params: asv's parameter conventions, shared by the adapters whose benchmarks take them."""

from pathlib import Path

from benchx.adapters import asv_runtime, benchx_params as params


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
