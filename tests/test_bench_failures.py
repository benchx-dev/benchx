"""What `bx bench` says about a result that is not a measurement (FAILURE_MESSAGES_PLAN.md)."""

import textwrap

import pytest

from benchx import bench, cli


def result(name="bench_boom", status="error", reason="harness.error", message="ValueError: boom", parameters=None):
    workload = {"name": name, **({"parameters": parameters} if parameters else {})}
    document = {
        "coordinates": {"workload": workload},
        "measurement": {"status": status, **({"reason": reason} if reason else {})},
        "provenance": {"info": {"message": message} if message else {}},
    }
    if status == "success":
        document["measurement"]["observations"] = [1.0, 2.0]
    return document


def rows_of(*documents):
    return [bench.row_of(d) for d in documents]


def test_a_row_carries_what_a_failed_result_said():
    assert bench.row_of(result())["message"] == "ValueError: boom"
    assert bench.row_of(result(status="skipped", reason="harness.skipped", message="no GPU"))["message"] == "no GPU"


def test_a_row_has_no_message_when_there_is_none_to_give():
    assert bench.row_of(result(status="success", reason=None, message="a note"))["message"] is None  # measured
    assert bench.row_of(result(message=None))["message"] is None  # failed without a word
    no_provenance = result()
    del no_provenance["provenance"]  # a result from another producer
    assert bench.row_of(no_provenance)["message"] is None


def test_nothing_is_said_when_every_case_was_measured():
    assert bench.failures([]) == ""
    assert bench.failures(rows_of(result(status="success", reason=None, message=None))) == ""
    assert bench.failures(rows_of(result(message=None))) == ""  # no message: the status column is all there is


def test_a_failed_case_is_named_with_what_it_said():
    assert bench.failures(rows_of(result())) == "\nnot measured (1)\n  bench_boom: ValueError: boom"


def test_cases_that_said_the_same_are_one_line_with_three_names_at_most():
    rows = rows_of(*(result("bench_boom", parameters={"n": str(n)}) for n in (1, 10, 100, 1000, 10000)))
    assert bench.failures(rows) == (
        "\nnot measured (5)\n  ValueError: boom (5 cases: bench_boom [n=1], bench_boom [n=10], bench_boom [n=100], …)"
    )
    three = rows_of(*(result("bench_boom", parameters={"n": str(n)}) for n in (1, 10, 100)))
    assert bench.failures(three).endswith("(3 cases: bench_boom [n=1], bench_boom [n=10], bench_boom [n=100])")


def test_different_messages_are_different_lines_in_the_order_they_came():
    rows = rows_of(
        result("a", message="KeyError: 'x'"),
        result("b", message="ValueError: boom"),
        result("c", status="skipped", reason="harness.skipped", message="KeyError: 'x'"),
    )
    assert bench.failures(rows) == ("\nnot measured (3)\n  KeyError: 'x' (2 cases: a, c)\n  b: ValueError: boom")


def test_a_message_is_one_line_and_not_longer_than_a_line_should_be():
    long = bench.failures(rows_of(result(message="ValueError: " + "x" * 300)))
    assert long.endswith("…") and len(long.splitlines()[-1]) == len("  bench_boom: ") + 160
    several = bench.failures(rows_of(result(message="RuntimeError: first\nsecond line\nthird")))
    assert several == "\nnot measured (1)\n  bench_boom: RuntimeError: first"


def test_bx_bench_says_why_a_benchmark_failed(tmp, monkeypatch, capsys):
    pytest.importorskip("pyperf")
    tree = tmp / "tree"
    tree.mkdir()
    (tree / "mod.py").write_text(
        textwrap.dedent("""
        def bench_ok():
            sum(range(100))

        def bench_boom():
            raise ValueError("array has no axis 3")
    """)
    )
    monkeypatch.chdir(tree)
    cli.main(
        [
            "bench",
            "mod.py",
            "--adapter",
            "pyperf",
            "--run-key",
            "boom",
            "--no-ingest",
            "--processes",
            "2",
            "--values",
            "2",
            "--loops",
            "20",
        ]
    )
    out = capsys.readouterr().out
    assert "error (harness.error)" in out  # the status column is as it was
    assert "\nnot measured (1)\n  bench_boom: ValueError: array has no axis 3\n" in out
    assert "bench_ok" not in out.split("not measured")[1]  # what was measured is not in it


TRACEBACK = """Traceback (most recent call last):
  File "driver.py", line 37, in <module>
    runner.bench_func('bench_axes', func, *args)
  File "mod.py", line 7, in bench_axes
    raise KeyError("axis")
{last}
"""
MANAGER = """Traceback (most recent call last):
  File "pyperf/_manager.py", line 118, in spawn_worker
    raise RuntimeError("%s failed with exit code %s"
RuntimeError: /usr/bin/python failed with exit code 1
"""
WARNING = """driver.py:34: SyntaxWarning: "is not" with 'int' literal. Did you mean "!="?
  if 0 is not None:
"""


def test_the_message_is_the_exception_that_ended_the_workers_traceback():
    from benchx.adapters import pyperf

    stderr = TRACEBACK.format(last="KeyError: 'axis'") + MANAGER
    assert pyperf._failure_message(stderr) == "KeyError: 'axis'"  # not the manager's wrapper after it


def test_a_warning_printed_before_the_traceback_is_not_the_message():
    from benchx.adapters import pyperf

    stderr = WARNING + WARNING + TRACEBACK.format(last="KeyError: 'axis'") + MANAGER
    assert pyperf._failure_message(stderr) == "KeyError: 'axis'"


def test_a_chained_exception_gives_the_one_that_was_raised_last():
    from benchx.adapters import pyperf

    chained = (
        TRACEBACK.format(last="KeyError: 'axis'")
        + "\nThe above exception was the direct cause of the following exception:\n\n"
        + TRACEBACK.format(last="ValueError: no such axis")
    )
    assert pyperf._failure_message(chained + MANAGER) == "ValueError: no such axis"


def test_the_wrapper_is_the_message_only_when_nothing_else_was_said():
    from benchx.adapters import pyperf

    assert pyperf._failure_message(MANAGER) == "RuntimeError: /usr/bin/python failed with exit code 1"


def test_without_a_traceback_the_first_line_said_is_the_message():
    from benchx.adapters import pyperf

    assert pyperf._failure_message("Killed\n") == "Killed"
    assert pyperf._failure_message(WARNING) == WARNING.splitlines()[0]  # nothing better was said
