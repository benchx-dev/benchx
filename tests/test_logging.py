"""The log: its switch, levels and file, and what `bx` shows beside it (logging_plan.md, Phase 1).

Google Benchmark runs against the fake binary, so these run anywhere.
"""

import argparse
import logging
import re
from pathlib import Path

import pytest

from benchx import cli, log, snapshot
from conftest import QUIET, NOISY, cases, make_build
from test_config import write_config

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z \d+ benchx\.[\w.]+ [A-Z]+ ")


@pytest.fixture
def build(repo, tmp):
    return make_build(tmp / "build", repo["wt-head"], cases())


def log_of(tmp, run_key) -> str:
    return (tmp / "results" / run_key / "bx.log").read_text()


def bench(build, *extra, key="g1"):
    return cli.main(["bench", str(build), *GBENCH, "--run-key", key, *extra])


# --- the default: stderr as before, and a log beside the results

def test_the_default_leaves_stderr_as_it_was_and_writes_the_log_in_the_run_folder(build, tmp, capsys):
    assert bench(build) == cli.EXIT_OK
    out = capsys.readouterr()
    assert not any(STAMP.match(line) for line in out.err.splitlines())  # no timestamped line on the terminal
    assert "5 repetitions" in out.err and QUIET in out.out and NOISY in out.out
    lines = log_of(tmp, "g1").splitlines()
    assert all(STAMP.match(line) for line in lines)
    assert " INFO start: benchx " in lines[0] and "bx bench" in lines[0] and str(tmp) in lines[0]
    assert " INFO end: ok (exit 0), " in lines[-1]
    assert len({line.split()[1] for line in lines}) == 1  # every line carries the one pid


def test_the_header_records_the_command_as_given_to_main(build, tmp):
    assert bench(build, "-e", "SECRET=hunter2") == cli.EXIT_OK
    assert "-e SECRET=hunter2" in log_of(tmp, "g1").splitlines()[0]  # as given: the log makes no claim to hide it


def test_no_log_writes_no_file_and_stderr_is_the_same(build, tmp, capsys):
    assert bench(build) == cli.EXIT_OK
    with_log = capsys.readouterr().err
    assert bench(build, "--no-log", key="g2") == cli.EXIT_OK
    assert capsys.readouterr().err == with_log
    assert not (tmp / "results" / "g2" / "bx.log").exists()


def test_a_run_key_with_a_slash_keeps_the_log_in_the_run_folder(build, tmp):
    assert bench(build, key="a/b") == cli.EXIT_OK
    assert (tmp / "results" / "a_b" / "bx.log").exists() and not (tmp / "results" / "a").exists()


def test_a_log_is_appended_to_under_a_new_header(build, tmp):
    assert bench(build, "--no-ingest") == cli.EXIT_OK
    assert bench(build, "--no-ingest") == cli.EXIT_OK
    text = log_of(tmp, "g1")
    assert text.count(" INFO start: ") == 2 and text.count(" INFO end: ") == 2


def test_the_commands_without_an_output_folder_write_no_log(tmp):
    for command in (["head"], ["series"], ["config", "show"]):
        assert cli.main(command) == cli.EXIT_OK
    assert not list(tmp.rglob("bx.log"))


def test_the_default_log_keeps_the_settings_with_their_sources(build, tmp):
    assert bench(build, "--repetitions", "3", key="g3") == cli.EXIT_OK
    text = log_of(tmp, "g3")
    assert " DEBUG setting adapter: google-benchmark (flag)" in text
    assert " DEBUG setting precision: flags repetitions; file none" in text
    assert re.search(r" INFO config file: none found from \S+ upward; project root \S+; store \S+; results \S+", text)


def test_the_adapter_flag_over_a_same_family_file_names_both_in_the_log(build, tmp):
    write_config(tmp, adapter={"name": "pyperf", "suite": "m.py"})
    cli.main(["bench", str(build), "--adapter", "asv", "--suite", "gone", "--run-key", "r19"])  # refused later
    assert " DEBUG setting adapter: asv (flag; the file says pyperf, same family)" in log_of(tmp, "r19")


# --- refusals: the folder exists only once the command's own checks have passed

@pytest.mark.parametrize("argv", [
    ["--processes", "2"], ["--adapter", "pyperf", "--suite", "m.py", "--repetitions", "3"], ["--tree", "t"]])
def test_a_refusal_for_a_flag_leaves_no_folder(build, tmp, argv, capsys):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "r1", *argv]) == cli.EXIT_REFUSED
    assert "bx bench: error:" in capsys.readouterr().err
    assert not (tmp / "results").exists()


def test_a_refusal_inside_the_session_leaves_the_folder_holding_only_the_log(build, tmp, capsys):
    assert cli.main(["bench", str(build), "--adapter", "google-benchmark", "--suite", "gone", "--run-key", "r2"]) \
        == cli.EXIT_REFUSED
    assert "suite binary not found" in capsys.readouterr().err
    assert [p.name for p in (tmp / "results" / "r2").iterdir()] == ["bx.log"]
    text = log_of(tmp, "r2")
    assert re.search(r" ERROR bx bench: error: suite binary not found", text)
    assert " INFO end: refused (exit 2), " in text


# --- the switch: flags, and an unusable file

@pytest.mark.parametrize("command", [["head"], ["bench", "x", "--adapter", "pyperf", "--suite", "s"], ["validate", "f.json"]])
def test_a_log_file_ending_in_json_is_refused_by_every_command(command, tmp, capsys):
    assert cli.main([*command, "--log-file", "out.json"]) == cli.EXIT_REFUSED
    assert f"bx {command[0]}: error: --log-file out.json: a log file must not end in .json" in capsys.readouterr().err
    assert not list(tmp.iterdir())


def test_a_log_file_that_cannot_be_opened_is_refused_before_anything_runs(tmp, capsys):
    assert cli.main(["head", "--log-file", str(tmp / "no" / "such" / "dir" / "bx.log")]) == cli.EXIT_REFUSED
    assert "cannot open" in capsys.readouterr().err


def test_log_file_names_a_file_for_a_command_with_no_output_folder(tmp, capsys):
    path = tmp / "mine.log"
    assert cli.main(["head", "--log-file", str(path)]) == cli.EXIT_OK
    text = path.read_text()
    assert " INFO start: benchx " in text and "bx head --log-file" in text and " INFO end: ok (exit 0)" in text
    assert "0 result(s)" in capsys.readouterr().out  # stdout is the command's answer, not a log line


def test_the_out_placeholder_needs_an_output_folder_when_a_flag_uses_it(tmp, capsys):
    assert cli.main(["head", "--log-file", "{out}/x.log"]) == cli.EXIT_REFUSED
    assert "bx head has no output folder" in capsys.readouterr().err


def test_the_log_flag_sends_the_whole_log_to_stderr_and_stdout_stays_clean(build, tmp, capsys):
    assert bench(build, "--log") == cli.EXIT_OK
    out = capsys.readouterr()
    assert STAMP.match(out.err.splitlines()[0]) and " INFO end: ok (exit 0)" in out.err
    assert not any(STAMP.match(line) for line in out.out.splitlines())
    assert not (tmp / "results" / "g1" / "bx.log").exists()


def test_no_log_with_a_level_is_a_usage_error(tmp, capsys):
    assert cli.main(["head", "--no-log", "--log-level", "info"]) == cli.EXIT_REFUSED
    assert "--no-log with --log-level" in capsys.readouterr().err


def test_the_three_switch_flags_exclude_each_other():
    with pytest.raises(SystemExit):
        cli.main(["head", "--log", "--no-log"])


# --- levels

def test_a_log_level_from_the_environment_drops_what_is_below_it(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_LOG_LEVEL", "info")
    assert bench(build) == cli.EXIT_OK
    text = log_of(tmp, "g1")
    assert " INFO start: " in text and " DEBUG " not in text


def test_the_flag_beats_the_environment_for_the_level(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_LOG_LEVEL", "info")
    assert bench(build, "--log-level", "DEBUG") == cli.EXIT_OK
    assert " DEBUG " in log_of(tmp, "g1")


def test_a_bad_level_in_the_environment_is_refused_naming_the_variable(build, tmp, monkeypatch, capsys):
    monkeypatch.setenv("BENCHX_LOG_LEVEL", "loud")
    assert bench(build) == cli.EXIT_REFUSED
    assert "bx bench: error: BENCHX_LOG_LEVEL=loud: not one of debug, info, warning, error" in capsys.readouterr().err
    assert not (tmp / "results").exists()


def test_the_level_and_the_switch_come_from_the_file_too(build, tmp):
    write_config(tmp, log_level="warning", logging="{root}/mine.log")
    assert bench(build) == cli.EXIT_OK
    assert not (tmp / "results" / "g1" / "bx.log").exists()
    text = (tmp / "mine.log").read_text()
    assert text.strip() == "" or " INFO " not in text and " DEBUG " not in text  # only WARNING and above


def test_verbose_shows_every_info_record_on_the_terminal_and_quiet_hides_the_progress(build, tmp, capsys):
    assert bench(build, "-v") == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "start: benchx" in err and "config file:" in err and not any(STAMP.match(l) for l in err.splitlines())
    assert bench(build, "-q", key="g2") == cli.EXIT_OK
    assert "5 repetitions" not in capsys.readouterr().err  # the progress line is INFO


def test_verbosity_from_the_environment_and_its_refusal(build, monkeypatch, capsys):
    monkeypatch.setenv("BENCHX_VERBOSE", "2")
    assert bench(build, "--no-log") == cli.EXIT_OK
    assert " DEBUG " not in capsys.readouterr().err  # the bare message, not the log's line
    monkeypatch.setenv("BENCHX_VERBOSE", "loud")
    assert bench(build) == cli.EXIT_REFUSED
    assert "bx bench: error: BENCHX_VERBOSE=loud: not a whole number from -2 to 2" in capsys.readouterr().err


def test_config_show_has_the_three_rows_with_their_sources(monkeypatch, capsys):
    monkeypatch.setenv("BENCHX_VERBOSE", "1")
    assert cli.main(["config", "show"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert '"{out}/bx.log"  (default)' in out
    assert "log level                  debug  (default)" in out
    assert "verbosity                  1  (environment BENCHX_VERBOSE)" in out


# --- the wrapper: crashes, exits and interrupts reach the footer

def test_a_crash_is_a_console_error_with_its_traceback_and_a_footer(tmp, monkeypatch, capsys):
    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "Store", boom)  # inside the command, after it has started the log
    path = tmp / "crash.log"
    assert cli.main(["series", "--log-file", str(path)]) == 1  # the status an uncaught exception gives
    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err and err.rstrip().endswith("RuntimeError: boom")
    text = path.read_text()
    assert " ERROR Traceback (most recent call last)" in text and "    RuntimeError: boom" in text
    assert " INFO end: crashed (exit 1), " in text


def test_system_exit_passes_through_with_a_footer(tmp, monkeypatch):
    def leave(*args):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "Store", leave)
    path = tmp / "exit.log"
    with pytest.raises(SystemExit) as exited:
        cli.main(["series", "--log-file", str(path)])
    assert exited.value.code == 2
    assert " INFO end: refused (exit 2), " in path.read_text()


def test_an_interrupt_is_logged_and_re_raised(tmp, monkeypatch):
    def stop(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Store", stop)
    path = tmp / "int.log"
    with pytest.raises(KeyboardInterrupt):
        cli.main(["series", "--log-file", str(path)])
    text = path.read_text()
    assert " WARNING interrupted" in text and re.search(r" INFO end: interrupted, ", text)


def test_a_usage_error_leaves_no_log_and_no_footer(tmp):
    with pytest.raises(SystemExit):
        cli.main(["bench"])
    assert not list(tmp.rglob("*.log"))


def test_main_leaves_no_handler_behind(build):
    assert bench(build) == cli.EXIT_OK
    handlers = logging.getLogger("benchx").handlers
    assert all(isinstance(h, logging.NullHandler) for h in handlers)


# --- the file: one line format, several-line messages, and the tree it sits in

def test_a_message_of_several_lines_is_one_prefixed_line_and_indented_continuations(tmp):
    path = tmp / "multi.log"
    log.start_console(0)
    log.configure(path, argv=["x"])
    try:
        log.console("benchx.cli").error("first line\nsecond line\nthird line")
    finally:
        log.stop()
    lines = path.read_text().splitlines()
    assert STAMP.match(lines[1]) and lines[1].endswith(" ERROR first line")
    assert lines[2:4] == ["    second line", "    third line"]


def test_the_console_handler_writes_a_multi_line_message_verbatim(capsys):
    log.start_console(0)
    try:
        log.console("benchx.cli").error("first line\nsecond line")
    finally:
        log.stop()
    assert capsys.readouterr().err == "first line\nsecond line\n"


def test_the_log_file_is_left_out_of_every_tree_identity(build, tmp):
    path = tmp / "wt.log"
    args = argparse.Namespace(log=str(path), log_level=None, argv=[], verbosity=0, verbosity_source="default",
                              out=None)
    cfg = cli._load("bench")
    try:
        started = cli._start_logging(cfg, args, None, "bench")
        assert started.log_file == path.resolve() and path.resolve() in snapshot._IGNORED
        assert path.resolve() in started.own_paths
    finally:
        log.stop()


def test_the_command_helper_gives_a_pastable_line_and_shortens_inline_code():
    script = "import sys\nprint(1)\n"
    text = log.command(["python", "-c", script, "it's"], scripts={"LISTER": script}, temporary=["/tmp/x"])
    assert text == "python -c '<LISTER: 2 lines>' 'it'\"'\"'s'"
    assert log.command(["a", "/tmp/x/f"], temporary=["/tmp/x"]).endswith("(temporary, removed after the call: /tmp/x)")


def test_once_is_true_the_first_time_after_configure(tmp):
    log.configure(False)
    assert log.once("k") and not log.once("k")
    log.configure(False)
    assert log.once("k")


def test_degraded_phrases_end_the_footer_and_are_warnings(tmp):
    path = tmp / "d.log"
    log.start_console(0)
    log.configure(path, argv=[])
    try:
        log.degraded("1 of 2 cases not measured")
        log.footer("ok", 0)
    finally:
        log.stop()
    text = path.read_text()
    assert " WARNING 1 of 2 cases not measured" in text
    assert re.search(r" INFO end: ok \(exit 0\), [\d.]+ s, 1 of 2 cases not measured\n$", text)
