"""`-q` and `-qq` on a run, and the run's report in the log (logging_plan.md §4: the console marker,
extended to stdout). Google Benchmark runs against the fake binary, so these run anywhere."""

import json
import re
import sys

import pytest

from benchx import bench as bench_mod
from benchx import cli, log
from conftest import cases, make_build

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \d+ benchx(\.\w+)+ [A-Z]+ ")
HEADER = "mean ± sd"  # the table's header line, which no other line has
START = r"measuring demo-bench on \S+ \(round 1/5, slot 3\)"  # the line a call starts with, with the total
POSITION = ["--round", "1/5", "--slot", "3"]


@pytest.fixture
def build(repo, tmp):
    """Four cases: one fails and one is skipped (both are listed under `not measured`, only the failure counts)."""
    return make_build(tmp / "build", repo["wt-head"], cases(), error=["BM_Broken"], skip=["BM_Skipped"])


@pytest.fixture
def clean(repo, tmp):
    return make_build(tmp / "clean", repo["wt-head"], cases())


def bench(build, capsys, *extra, key="r1"):
    code = cli.main(["bench", str(build), *GBENCH, "--run-key", key, *extra])
    seen = capsys.readouterr()
    return code, seen.out, seen.err


def log_of(tmp, key="r1") -> list[str]:
    return (tmp / "results" / key / "bx.log").read_text().splitlines()


def test_the_default_stdout_is_the_table_the_failures_and_the_summary(build, capsys):
    code, out, err = bench(build, capsys, *POSITION)
    assert code == cli.EXIT_OK
    assert out.splitlines()[0].startswith("case ") and HEADER in out
    assert re.search(r"\n\nnot measured \(2\)\n  BM_Broken: .+\n  BM_Skipped: .+\n\n"
                     r"\d+ result\(s\) -> \S+  \(order sha256:\w+\)\n", out)
    assert "ingested" not in out  # a progress message now, and on stderr with the others
    assert re.fullmatch(START + r"\nprecision: .*\n\S+store\.parquet: ingested \d+, duplicate 0\n", err)


def test_q_leaves_the_failures_and_the_result_line_and_no_table(build, capsys):
    code, out, err = bench(build, capsys, "-q", *POSITION)
    assert code == cli.EXIT_OK
    lines = out.splitlines()
    assert HEADER not in out and "" not in lines  # no table, and no spacing between what is left
    assert lines[0] == "not measured (2)" and lines[1].startswith("  BM_Broken: ")
    assert lines[2].startswith("  BM_Skipped: ")
    assert re.fullmatch(r"\d+ result\(s\) -> \S+  \(order sha256:\w+\)", lines[3]) and len(lines) == 4
    assert re.fullmatch(START + "\n", err)  # on stderr, the start line: not the progress line or the ingest line


def test_qq_leaves_only_the_failures(build, capsys):
    code, out, err = bench(build, capsys, "-qq", *POSITION)
    assert code == cli.EXIT_OK  # the exit status stays 0: a failed case is a result
    assert out.splitlines()[0] == "not measured (2)" and len(out.splitlines()) == 3
    assert err == ""


def test_qq_on_a_run_that_succeeded_is_silent(clean, capsys):
    code, out, err = bench(clean, capsys, "-qq")
    assert (code, out, err) == (cli.EXIT_OK, "", "")


def test_q_on_a_run_that_succeeded_is_a_start_line_and_a_result_line(clean, capsys):
    code, out, err = bench(clean, capsys, "-q", *POSITION)
    lines = out.splitlines()
    assert code == cli.EXIT_OK and re.fullmatch(START + "\n", err) and len(lines) == 1
    assert "result(s) ->" in lines[0]


def test_the_start_line_says_where_a_call_sits_and_what_it_measures(clean, capsys):
    _, _, err = bench(clean, capsys, "-q", "--label", "side=base", "--round", "2", "--slot", "5")
    assert re.fullmatch(r"measuring demo-bench on \S+ \(round 2, slot 5, side=base\)\n", err)  # no total given
    _, _, err = bench(clean, capsys, "-q", "--round", "4/5", "--slot", "9", key="r3")  # the last of 5, numbered from 0
    assert re.fullmatch(r"measuring demo-bench on \S+ \(round 4/5, slot 9\)\n", err)
    _, _, err = bench(clean, capsys, "-q", key="r2")  # alone in its session: no position to give
    assert re.fullmatch(r"measuring demo-bench on \S+\n", err)


def test_the_start_line_is_said_once_with_v_and_is_in_the_log_whatever_the_terminal_shows(build, tmp, capsys):
    _, _, err = bench(build, capsys, "-v", *POSITION)
    assert len(re.findall(START, err)) == 1
    for number, flags in enumerate([[], ["-q"], ["-qq"]]):
        key = f"s{number}"
        bench(build, capsys, *flags, *POSITION, key=key)
        assert any(re.search(r" INFO " + START + "$", line) for line in log_of(tmp, key))


def test_a_failure_with_no_message_is_one_line_when_the_table_is_hidden(build, capsys, monkeypatch):
    monkeypatch.setattr(bench_mod, "failures", lambda rows: "")  # the status column was all there was
    one_line = r"\d+ of \d+ cases not measured"
    _, out, _ = bench(build, capsys, "-qq")
    assert re.fullmatch(one_line + "\n", out)
    _, out, _ = bench(build, capsys, key="r2")  # the table says it, so no line is added
    assert not re.search(one_line, out) and "error (harness.error)" in out


@pytest.mark.parametrize("flags", [[], ["-q"], ["-qq"], ["-v"]])
def test_the_log_holds_the_report_whatever_the_terminal_shows(build, tmp, capsys, flags):
    bench(build, capsys, *flags)
    lines = log_of(tmp)
    assert all(STAMP.match(line) for line in lines)  # a line a record, as the harness output is logged
    table = [i for i, line in enumerate(lines) if HEADER in line]
    assert len(table) == 1 and " INFO " in lines[table[0]]
    rows = lines[table[0]:table[0] + 6]
    assert any("BM_Quiet/1024" in row for row in rows) and any("BM_Broken" in row for row in rows)
    assert any(" WARNING not measured (2)" in line for line in lines)  # the errors, as a warning
    assert any(re.search(r" INFO \d+ result\(s\) -> ", line) for line in lines)
    assert any(re.search(r" INFO \S+store\.parquet: ingested \d+, duplicate 0$", line) for line in lines)  # once


def test_the_log_level_decides_what_of_it_the_log_keeps(build, tmp, capsys):
    bench(build, capsys, "--log-level", "warning")
    text = "\n".join(log_of(tmp))
    assert HEADER not in text and " WARNING not measured (2)" in text


def test_v_does_not_say_the_report_twice(build, capsys):
    _, out, err = bench(build, capsys, "-v")
    assert HEADER in out and HEADER not in err  # stdout carries it already


def test_with_the_log_on_stderr_the_report_is_on_stdout_and_once_in_the_log(build, capsys):
    _, out, err = bench(build, capsys, "--log")
    assert HEADER in out
    assert sum(HEADER in line for line in err.splitlines()) == 1 and STAMP.match(
        next(line for line in err.splitlines() if HEADER in line))


def test_the_ingest_command_prints_its_counts_at_every_verbosity(clean, tmp, capsys):
    assert bench(clean, capsys, "--no-ingest", "--out", str(tmp / "o"))[0] == cli.EXIT_OK
    assert cli.main(["ingest", str(tmp / "o"), "-qq"]) == cli.EXIT_OK
    assert re.fullmatch(r"ingested \d+, duplicate 0\n", capsys.readouterr().out)  # its answer, not a summary


def test_bx_run_is_quiet_by_the_same_tiers(clean, tmp, capsys):
    bench(clean, capsys, "--no-ingest", "--out", str(tmp / "o"))
    order = next((tmp / "o").rglob("orders/order*.json"))
    assert cli.main(["run", str(order), "--out", str(tmp / "o1"), "-q"]) == cli.EXIT_OK
    seen = capsys.readouterr()
    lines = seen.out.splitlines()
    assert len(lines) == 1 and "result(s) for" in lines[0]
    assert re.fullmatch(r"running the order order\S*\.json\n", seen.err)
    # A second run of the order would meet its own results in the store: leave it out of this one.
    assert cli.main(["run", str(order), "--out", str(tmp / "o2"), "-qq", "--no-ingest"]) == cli.EXIT_OK
    assert capsys.readouterr() == ("", "")


def test_a_failed_write_to_stdout_is_the_commands_failure_as_with_print(monkeypatch):
    class Closed:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            pass

    log.start_console(0)
    try:
        monkeypatch.setattr(sys, "stdout", Closed())
        with pytest.raises(BrokenPipeError):  # logging would have swallowed it
            log.console("benchx.cli").out("x")
    finally:
        log.stop()


def test_a_percent_sign_in_the_text_is_left_alone(capsys):
    log.start_console(0)
    try:
        log.console("benchx.cli").out("50% of %s and %d")
    finally:
        log.stop()
    assert capsys.readouterr().out == "50% of %s and %d\n"


def test_q_with_the_log_on_stderr_still_hides_the_report_on_stdout(build, capsys):
    _, out, err = bench(build, capsys, "--log", "-qq")
    assert HEADER not in out and out.splitlines()[0] == "not measured (2)"
    assert HEADER in err  # stderr is the log, which -q leaves alone


def test_the_total_of_rounds_is_for_the_start_line_only(clean, tmp, capsys):
    """The work order records the round as it always did: 1, not 1/5."""
    bench(clean, capsys, "--round", "1/5", "--slot", "3", "--no-ingest")
    order = json.loads(next((tmp / "results" / "r1" / "orders").glob("order-0003.json")).read_text())
    assert order["round"] == 1 and order["slot"] == 3 and "rounds" not in order
