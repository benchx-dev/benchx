"""The order-side harness specs used by bx bench and bx compare."""

import pytest

from benchx import adapters, harnesses
from benchx.errors import SessionError


def test_every_harness_has_a_runner_adapter():
    assert set(harnesses.HARNESSES) <= set(adapters.ADAPTERS)


def test_unknown_harness_is_refused():
    with pytest.raises(SessionError, match="no harness"):
        harnesses.get("no-such-harness")


def test_exact_filter_matches_names_exactly():
    assert harnesses.exact_filter([]) is None
    assert harnesses.exact_filter(["f", "a.b"]) == r"^(f|a\.b)$"


@pytest.mark.parametrize("harness", list(harnesses.HARNESSES.values()))
def test_suite_entry_leaves_out_an_empty_filter(harness):
    assert harness.suite_entry("s") == {"adapter": harness.name, "suite": "s"}
    assert harness.suite_entry("s", "^x$")["filter"] == "^x$"


def test_google_benchmark_precision():
    gb = harnesses.GOOGLE_BENCHMARK
    assert gb.precision({})[0] == {"repetitions": 5}
    flags = gb.flags_to_precision(repetitions=3, min_time=0.5)
    assert gb.precision(flags) == ({"repetitions": 3, "calibration": {
        "mode": "adaptive", "minimum_sample_seconds": 0.5}}, None)


@pytest.mark.parametrize("harness, flag", [(harnesses.GOOGLE_BENCHMARK, "processes"),
                                           (harnesses.PYPERF, "repetitions")])
def test_a_flag_the_harness_does_not_take_is_refused(harness, flag):
    with pytest.raises(SessionError, match=f"takes no --{flag}"):
        harness.flags_to_precision(**{flag: 2})


def test_unset_flags_are_not_refused():
    assert harnesses.PYPERF.flags_to_precision(repetitions=None) == {}


def test_pyperf_calibration_is_one_or_the_other():
    with pytest.raises(SessionError, match="both set calibration"):
        harnesses.PYPERF.flags_to_precision(min_time=0.1, loops=5)


def test_pyperf_precision_falls_back_to_its_defaults(tmp_path):
    precision, path = harnesses.PYPERF.precision({}, start=tmp_path)
    assert path is None
    assert precision == adapters.get("pyperf").default_precision()
