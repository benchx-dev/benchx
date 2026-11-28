"""What the log says about a run: the order, each case, the files, the harness output, the end.
Google Benchmark runs against the fake binary, so these run anywhere."""

import logging
import os
import re
import sys

import pytest

from benchx import cli, log, runner
from benchx.adapters import gbench
from conftest import cases, make_build

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]


@pytest.fixture
def build(repo, tmp):
    return make_build(tmp / "build", repo["wt-head"], cases(), error=["BM_Broken"], skip=["BM_Skipped"])


def bench(build, *extra, key="r1"):
    return cli.main(["bench", str(build), *GBENCH, "--run-key", key, *extra])


def lines_of(tmp, key="r1") -> list[str]:
    return (tmp / "results" / key / "bx.log").read_text().splitlines()


def record(lines, level, needle):
    """The first line at `level` that contains `needle`."""
    return next(line for line in lines if f" {level} " in line and needle in line)


def test_the_log_names_the_order_and_each_case_before_and_after_it_runs(build, tmp):
    assert bench(build) == cli.EXIT_OK
    lines = lines_of(tmp)
    assert re.search(r" INFO order sha256:\w+: google-benchmark, 4 case\(s\)$", record(lines, "INFO", "order sha256"))
    assert " INFO listing the benchmarks of demo-bench" in "\n".join(lines)
    for number in range(1, 5):
        start = next(i for i, l in enumerate(lines) if f"case {number}/4 " in l and ": starting, limit 60 s" in l)
        done = next(i for i, l in enumerate(lines) if f"case {number}/4 " in l and ", exit 0," in l)
        assert start < done  # the start is written before the case runs, the line after it
    assert re.search(
        r" INFO case \d/4 BM_Quiet/1024: success, exit 0, [\d.]+ s, load [\d.n/a]+$", "\n".join(lines), re.M
    )


def test_a_case_that_failed_is_a_warning_with_its_message_and_its_output(build, tmp):
    assert bench(build) == cli.EXIT_OK  # a failed case is a result, and the command's exit status stays 0
    lines = lines_of(tmp)
    broken = record(lines, "WARNING", "BM_Broken: error (harness.error)")
    assert re.search(r"error \(harness\.error\): fake failure, exit 0, [\d.]+ s", broken)
    skipped = record(lines, "INFO", "BM_Skipped: skipped (harness.skipped)")  # expected: not a warning
    assert skipped.endswith("fake skip, exit 0, " + skipped.rsplit("exit 0, ", 1)[1])
    assert any(
        " WARNING " in l and "BM_Broken: stdout| " in l or " WARNING " in l and "BM_Broken: stdout, " in l
        for l in lines
    )  # a failed case's output is a WARNING, a successful one's DEBUG
    assert any(" DEBUG " in l and "BM_Quiet/1024: stdout, " in l for l in lines)


def test_the_end_of_the_order_and_the_footer_say_what_was_left_undone(build, tmp):
    assert bench(build) == cli.EXIT_OK
    lines = lines_of(tmp)
    order = record(lines, "WARNING", "results: ")  # an error result raises the line
    assert "error" in order and "skipped" in order and "success" in order
    assert re.search(r" INFO end: ok \(exit 0\), [\d.]+ s, 1 of 4 cases not measured$", lines[-1])
    assert " WARNING 1 of 4 cases not measured" in "\n".join(lines)  # --log-level warning keeps the phrase


def test_the_files_line_names_what_the_case_left_with_absolute_paths(build, tmp):
    assert bench(build) == cli.EXIT_OK
    files = record(lines_of(tmp), "DEBUG", "case 1/4 BM_Quiet/1024 files: ")
    for kind in ("native-output", "stdout", "stderr"):
        assert f"{kind} {tmp}/results/r1/artifacts/" in files
    assert re.search(r"results 2 " + re.escape(str(tmp)) + r"/results/r1/r1_BM_Quiet", files)


def test_a_discovery_call_is_logged_with_its_command_and_how_it_ended(build, tmp):
    assert bench(build) == cli.EXIT_OK
    text = "\n".join(lines_of(tmp))
    assert re.search(
        r"benchx\.adapters\.base DEBUG listing the benchmarks of demo-bench: \S+demo-bench "
        r"--benchmark_list_tests=true \(in .+, limit 600 s\)",
        text,
    )
    assert "listing the benchmarks of demo-bench: exit 0 in " in text


def test_a_rerun_of_a_run_key_says_that_its_files_are_overwritten(build, tmp, capsys):
    assert bench(build) == cli.EXIT_OK
    capsys.readouterr()
    assert bench(build) == cli.EXIT_INVALID  # the store refuses the second draw of every result
    lines = lines_of(tmp)
    second = lines[next(i for i, l in enumerate(lines) if " INFO start: " in l and i > 0) :]
    assert any(" WARNING " in l and "exists and is overwritten" in l for l in second)
    assert any(
        " WARNING " in l and "result file(s)" in l and "were overwritten by a rerun of run key r1" in l for l in second
    )
    assert " INFO end: invalid (exit 1)" in second[-1]


def test_an_interrupt_after_a_case_says_how_many_were_written_and_how_to_store_them(build, tmp, monkeypatch):
    original, calls = gbench.run_case, []

    def interrupted(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return original(*args, **kwargs)

    monkeypatch.setattr(gbench, "run_case", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bench(build)
    text = "\n".join(lines_of(tmp))
    out = tmp / "results" / "r1"
    assert f" WARNING 1 of 4 cases written to {out}, not ingested; bx ingest {out} stores them" in text
    assert " INFO end: interrupted, " in text


def test_a_refusal_lists_its_problems_in_the_log_before_it_is_raised(build, tmp, capsys):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "r1", "--filter", "NoSuchCase"]) == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "refused: " in err and "selects no case" in err
    lines = lines_of(tmp)
    assert any(" ERROR refused: " in l for l in lines) and " INFO end: refused (exit 2)" in lines[-1]
    assert any(" DEBUG refused with 1 problem(s): " in l for l in lines)


# --- the helpers


def result(status, reason=None, message=None):
    info = {"message": message} if message else {}
    return status, reason, {"provenance": {"info": info}, "measurement": {"status": status}}


def case_line(results, warnings=(), exit_status=0, timed_out=False, load=0.42):
    return runner._case_line(results, list(warnings), {"timed_out": timed_out, "exit_status": exit_status}, 41.2, load)


def test_a_case_line_is_info_unless_the_case_did_badly():
    level, text, bad = case_line([result("success")], ["setup-per-call-unbatched"])
    assert (level, bad) == (logging.INFO, False)
    assert text == "success [setup-per-call-unbatched], exit 0, 41.2 s, load 0.42"
    assert case_line([result("skipped", "harness.skipped", "no GPU")])[:2] == (
        logging.INFO,
        "skipped (harness.skipped): no GPU, exit 0, 41.2 s, load 0.42",
    )
    assert case_line([result("success")], load=None)[1].endswith("load n/a")


def test_a_non_zero_exit_raises_the_level_though_the_status_is_success():
    level, text, bad = case_line([result("success")], exit_status=1)
    assert (level, bad) == (logging.WARNING, True) and text.startswith("success, exit 1, ")


def test_an_error_a_timeout_and_a_doubtful_environment_are_warnings():
    assert case_line([result("error", "harness.error", "ValueError: boom")])[:2] == (
        logging.WARNING,
        "error (harness.error): ValueError: boom, exit 0, 41.2 s, load 0.42",
    )
    level, text, bad = case_line([result("error", "harness.timeout")], exit_status=None, timed_out=True)
    assert (level, bad) == (logging.WARNING, True) and ", timeout, " in text
    assert case_line([result("success")], ["env-mismatch"])[0] == logging.WARNING  # the line, not the output
    assert case_line([result("success")], ["env-mismatch"])[2] is False


def test_what_is_the_same_in_every_order_is_info_once_and_debug_after(caplog):
    log.configure(False)
    source = {"path": "/p", "revision": "a" * 40, "dirty": "dirty", "tree": "b" * 40, "changes": [" M a.py", "?? b.py"]}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        runner._log_identity({"python": "/venv/bin/python"}, source, None, "/p")
        runner._log_identity({"python": "/venv/bin/python"}, source, None, "/p")
    by_level = [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if "run under" in r.getMessage() or "tree is dirty" in r.getMessage()
    ]
    assert [level for level, _ in by_level] == ["INFO", "INFO", "DEBUG", "DEBUG"]
    assert any("M a.py; ?? b.py" in m and "dirty-subject-tree" in m for _, m in by_level)


def test_a_dirty_state_that_is_unknown_is_a_warning_naming_the_cause(caplog):
    log.configure(False)
    source = {"path": "/p", "revision": "a" * 40, "dirty": "unknown", "why_unknown": "git status failed"}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        runner._log_identity({}, source, None, "/p")
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "dirty state is unknown (git status failed)" in warned[0].getMessage()


def test_harness_output_keeps_the_last_forty_lines_and_says_where_the_rest_is(caplog, tmp):
    text = "\n".join(f"line {n}" for n in range(1, 101))
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        log.harness_output(
            logging.getLogger("benchx.test"), "case 1/1 x", "stderr", text, failed=True, kept_at=tmp / "0000.stderr"
        )
        log.harness_output(logging.getLogger("benchx.test"), "case 1/1 x", "stdout", "  \n", failed=True)  # empty
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == f"case 1/1 x: stderr, 100 lines (last 40; all in {tmp}/0000.stderr)"
    assert messages[1] == "case 1/1 x: stderr| line 61" and messages[-1] == "case 1/1 x: stderr| line 100"
    assert len(messages) == 41 and all(r.levelno == logging.WARNING for r in caplog.records)


def test_a_failing_collector_is_a_warning_with_its_reason_and_the_progress_is_info(caplog, tmp):
    from benchx import collectors

    specs = [
        {"name": "good", "command": [sys.executable, "-c", "print('{\"x\": 1}')"]},
        {
            "name": "bad",
            "command": [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
            "on_fail": "warn",
        },
    ]
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        collected = collectors.run_all(specs, declared=None, cwd=tmp, env=dict(os.environ), artifacts_dir=tmp)
    assert collected.warnings == ["collector-failed"]
    messages = {(r.levelname, r.getMessage()) for r in caplog.records}
    assert any(level == "INFO" and m.startswith("collector 1/2 good: ok, ") for level, m in messages)
    assert any(level == "INFO" and m.startswith("collector 2/2 bad: failed, ") for level, m in messages)
    assert ("WARNING", "collector bad failed: exit 3: boom") in messages
    assert any(level == "WARNING" and m == "collector bad: stderr| boom" for level, m in messages)


def test_a_bench_with_a_failed_case_keeps_the_not_measured_section_on_stdout_with_the_log_on(build, capsys):
    assert bench(build, "--log") == cli.EXIT_OK
    out = capsys.readouterr()
    assert re.search(r"not measured \(\d+\)", out.out) and "BM_Broken" in out.out
    assert not any(re.match(r"^\d{4}-\d\d-\d\dT", line) for line in out.out.splitlines())  # no log line on stdout
    assert any(re.match(r"^\d{4}-\d\d-\d\dT", line) for line in out.err.splitlines())  # the log is on stderr


def test_a_parametrized_case_logs_its_workload_and_parameter_values_before_it_runs(tmp, monkeypatch):
    pytest.importorskip("pyperf")
    import subprocess

    (tmp / "m.py").write_text(
        "def bench_matmul(n, kind):\n    sum(range(10))\n\n"
        "bench_matmul.params = [[10, 100], ['a', 'b']]\n"
        "bench_matmul.param_names = ['n', 'kind']\n"
    )
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"]):
        subprocess.run(["git", *command], cwd=tmp, check=True)
    assert (
        cli.main(
            [
                "bench",
                "m.py",
                "--run-key",
                "p1",
                "--no-ingest",
                "--processes",
                "2",
                "--values",
                "1",
                "--warmups",
                "0",
                "--loops",
                "1",
                "-e",
                "A=1",
                "bench_matmul",
            ]
        )
        == cli.EXIT_OK
    )
    lines = lines_of(tmp, "p1")
    listed = next(
        l for l in lines if " DEBUG " in l and "bench_matmul: 4 combination(s) of n: 10, 100, kind: 'a', 'b'" in l
    )
    first = next(i for i, l in enumerate(lines) if " INFO case 1/4 bench_matmul-0: starting" in l)
    shown = lines[first + 1]
    assert (
        " DEBUG case 1/4 bench_matmul-0: workload bench_matmul; parameters n=10, kind='a'; "
        "environment variables of the order A=1" in shown
    )
    assert "n=100, kind='b'" in next(l for l in lines if "case 4/4 bench_matmul-3: workload" in l)
    assert lines.index(listed) < first  # listed once, before the first case


def test_untracked_paths_are_said_once_as_info_and_capped(caplog):
    """The identity covers tracked files only; the log names what it leaves out (schema §4.1), once per tree."""
    log.configure(False)
    untracked = [f"file{n}.txt" for n in range(12)]
    source = {"path": "/p", "revision": "a" * 40, "dirty": "clean", "tree": "b" * 40, "untracked": untracked}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        runner._log_identity({}, source, None, "/p")
        runner._log_identity({}, source, None, "/p")
        runner._log_identity({}, {**source, "path": "/q"}, None, "/q")  # another tree: said again
        runner._log_identity({}, {"path": "/r", "revision": "a" * 40, "dirty": "clean", "tree": "c" * 40}, None, "/r")
    said = [(r.levelname, r.getMessage()) for r in caplog.records if "untracked" in r.getMessage()]
    assert [level for level, _ in said] == ["INFO", "INFO"]  # a tree with none says nothing
    message = said[0][1]
    assert message.startswith("the subject tree has 12 untracked path(s), which its dirty state and tree id leave out")
    assert "file0.txt, file1.txt" in message and "file9.txt, and 2 more" in message and "file10.txt" not in message
