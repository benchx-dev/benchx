"""`-q` and `-qq` on a run, and the run's report in the log (the console marker,
extended to stdout). Google Benchmark runs against the fake binary, so these run anywhere."""

import json
import re
import sys

import pytest

from benchx import bench as bench_mod
from benchx import cli, log
from conftest import HAS_PYARROW, cases, make_build

DEFAULT_STORE = re.escape("store.parquet" if HAS_PYARROW else "store.jsonl")

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
    assert re.search(
        r"\n\nnot measured \(2\)\n  BM_Broken: .+\n  BM_Skipped: .+\n\n"
        r"\d+ result\(s\) -> \S+  \(order sha256:\w+\)\n",
        out,
    )
    assert "ingested" not in out  # a progress message now, and on stderr with the others
    assert re.fullmatch(START + rf"\nprecision: .*\n\S+{DEFAULT_STORE}: ingested \d+, duplicate 0\n", err)


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
    monkeypatch.setattr(bench_mod, "said", lambda document: None)  # the status column was all there was
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
    rows = lines[table[0] : table[0] + 6]
    assert any("BM_Quiet/1024" in row for row in rows) and any("BM_Broken" in row for row in rows)
    assert any(" WARNING not measured (2)" in line for line in lines)  # the errors, as a warning
    assert any(re.search(r" INFO \d+ result\(s\) -> ", line) for line in lines)
    assert any(re.search(rf" INFO \S+{DEFAULT_STORE}: ingested \d+, duplicate 0$", line) for line in lines)  # once


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
        next(line for line in err.splitlines() if HEADER in line)
    )


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


# --- the cases that were not measured, which `bx run` says as `bx bench` does


def row(case, status, message=None):
    return {"case": case, "status": status, "message": message}


def report(rows, verbosity):
    log.start_console(verbosity)
    try:
        cli._report_unmeasured(rows)
    finally:
        log.stop()


def test_bx_run_reports_the_cases_that_were_not_measured_at_every_verbosity(build, tmp, capsys):
    bench(build, capsys, "--no-ingest", "--out", str(tmp / "o"))
    order = next((tmp / "o").rglob("orders/order*.json"))
    assert cli.main(["run", str(order), "--out", str(tmp / "o1"), "--no-ingest", "-qq"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "not measured (2)" and "BM_Broken: " in out  # it was silent before
    text = (tmp / "o1" / "bx.log").read_text()
    assert re.search(r" WARNING not measured \(2\)", text)
    assert re.search(r" INFO end: ok \(exit 0\), [\d.]+ s, 1 of \d+ cases not measured", text)  # the footer says so


def test_a_run_whose_only_messages_are_skips_reports_them_as_expected_not_as_a_warning(repo, tmp, capsys):
    skipping = make_build(tmp / "skipping", repo["wt-head"], cases(), skip=["BM_Skipped"])
    code, out, _ = bench(skipping, capsys)
    assert code == cli.EXIT_OK and "not measured (1)\n  BM_Skipped: " in out  # the default terminal shows it
    code, out, _ = bench(skipping, capsys, "-q", key="r2")
    assert "not measured" not in out  # nothing failed: it goes with the table, which -q hides
    code, out, err = bench(skipping, capsys, "-qq", key="r3")
    assert (out, err) == ("", "")
    text = "\n".join(log_of(tmp, "r3"))
    assert " INFO not measured (1)" in text and " WARNING not measured" not in text
    assert "cases not measured" not in text  # a skip is not an unmeasured case


def test_an_error_that_said_nothing_is_counted_when_a_skip_that_did_speak_is_what_the_table_has(capsys):
    rows = [row("a", "success"), row("b", "skipped", "no GPU"), row("c", "error (harness.error)")]
    report(rows, -1)  # -q: the table and the skip's message are hidden, and nothing names `c`
    assert capsys.readouterr().out == "1 of 3 cases not measured\n"
    report(rows, 0)  # the default terminal shows the skip, and the table (the caller's) shows `c`
    out = capsys.readouterr().out
    assert "b: no GPU" in out and "cases not measured" not in out


def test_an_error_that_said_nothing_is_counted_beside_one_that_did(capsys):
    rows = [row("a", "success"), row("b", "error (harness.error)", "boom"), row("c", "error (harness.error)")]
    report(rows, -2)
    assert capsys.readouterr().out == "not measured (1)\n  b: boom\n2 of 3 cases not measured\n"
