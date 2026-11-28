"""--harness-verbosity: how much the harness itself is asked to say. Off by default; one work-order field; the
`v`s past -vv ask for it too (-vvv is -vv and 1). Google Benchmark runs against the fake binary."""

import json
import subprocess

import pytest

from benchx import cli, core, runner
from conftest import cases, make_build
from test_config import write_config

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]


@pytest.fixture
def build(repo, tmp):
    return make_build(tmp / "build", repo["wt-head"], cases())


def bench(build, *extra, key="h1"):
    return cli.main(["bench", str(build), *GBENCH, "--run-key", key, "--no-ingest", *extra])


def order_of(tmp, key="h1") -> dict:
    return json.loads((tmp / "results" / key / "orders" / "order.json").read_text())


def log_of(tmp, key="h1") -> str:
    return (tmp / "results" / key / "bx.log").read_text()


def result_info(tmp, key="h1") -> dict:
    path = next((tmp / "results" / key).glob(f"{key}_BM_Quiet*wall-time.json"))
    return json.loads(path.read_text())["provenance"]["info"]


def test_by_default_the_harness_is_asked_for_nothing_more(build, tmp):
    assert bench(build) == cli.EXIT_OK
    assert "harness_verbosity" not in order_of(tmp) and "harness_verbosity" not in result_info(tmp)
    assert " --v=" not in log_of(tmp) and "harness is asked to say more" not in log_of(tmp)


def test_the_option_goes_into_the_order_the_command_line_and_the_result(build, tmp):
    assert bench(build, "--harness-verbosity", "2") == cli.EXIT_OK
    order = order_of(tmp)
    assert order["harness_verbosity"] == 2 and core.check(order, "work-order") == []
    text = log_of(tmp)
    assert "--benchmark_min_time=0.5s --v=2 (in " in text  # on the command line the log shows
    assert (
        " INFO the harness is asked to say more (harness_verbosity 2): its output is logged up to 2000 lines a stream"
        in text
    )
    assert result_info(tmp)["harness_verbosity"] == 2
    assert " DEBUG setting harness verbosity: 2 (flag)" in text


@pytest.mark.parametrize(
    "flags, level",
    [(["-vv"], 0), (["-vvv"], 1), (["-vvvv"], 2), (["-v", "-v", "-v", "-v", "-v"], 3), (["-vvvv", "-q"], 1)],
)
def test_each_v_past_two_asks_the_harness_to_say_more(build, tmp, flags, level, capsys):
    assert bench(build, *flags) == cli.EXIT_OK
    assert order_of(tmp).get("harness_verbosity", 0) == level
    assert (f"--v={level}" in log_of(tmp)) == bool(level)
    assert capsys.readouterr().err.count("DEBUG") == 0  # the terminal got -vv's bare messages, never more than that


def test_the_explicit_option_beats_the_vs(build, tmp):
    assert bench(build, "-vvvv", "--harness-verbosity", "5") == cli.EXIT_OK
    assert order_of(tmp)["harness_verbosity"] == 5
    assert " DEBUG setting harness verbosity: 5 (flag)" in log_of(tmp)


def test_the_environment_and_the_file_ask_too_in_that_order(build, tmp, monkeypatch):
    write_config(tmp, adapter={"name": "google-benchmark", "suite": "demo-bench", "verbosity": 1})
    assert cli.main(["bench", str(build), "--run-key", "h1", "--no-ingest"]) == cli.EXIT_OK
    assert order_of(tmp)["harness_verbosity"] == 1 and "harness verbosity: 1 (file)" in log_of(tmp)
    monkeypatch.setenv("BENCHX_HARNESS_VERBOSITY", "3")
    assert cli.main(["bench", str(build), "--run-key", "h2", "--no-ingest"]) == cli.EXIT_OK
    assert order_of(tmp, "h2")["harness_verbosity"] == 3
    assert "harness verbosity: 3 (environment BENCHX_HARNESS_VERBOSITY)" in log_of(tmp, "h2")
    assert cli.main(["bench", str(build), "--run-key", "h3", "--no-ingest", "--harness-verbosity", "0"]) == cli.EXIT_OK
    assert "harness_verbosity" not in order_of(tmp, "h3")  # a flag of 0 beats both: nothing more


def test_a_bad_value_is_refused_naming_where_it_came_from(build, tmp, monkeypatch, capsys):
    monkeypatch.setenv("BENCHX_HARNESS_VERBOSITY", "loud")
    assert bench(build) == cli.EXIT_REFUSED
    assert "bx bench: error: BENCHX_HARNESS_VERBOSITY=loud: not a whole number from 0" in capsys.readouterr().err
    monkeypatch.delenv("BENCHX_HARNESS_VERBOSITY")
    with pytest.raises(SystemExit):
        bench(build, "--harness-verbosity", "-1")
    assert "not a whole number from 0" in capsys.readouterr().err


def test_bx_verbose_from_the_environment_can_ask_too(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_VERBOSE", "4")  # the terminal's -vv, and two more
    assert bench(build) == cli.EXIT_OK
    assert order_of(tmp)["harness_verbosity"] == 2 and "verbosity: 2 (environment BENCHX_VERBOSE)" in log_of(tmp)


def test_config_show_has_the_row(monkeypatch, capsys):
    assert cli.main(["config", "show", "-vvv"]) == cli.EXIT_OK
    assert "harness verbosity          1  (flag: -v past -vv)" in capsys.readouterr().out


def test_the_log_keeps_more_of_the_output_when_the_harness_was_asked_for_more(build, tmp, monkeypatch):
    assert bench(build, "--harness-verbosity", "1", key="h1") == cli.EXIT_OK
    monkeypatch.setattr(runner, "VERBOSE_OUTPUT_LINES", 3)
    assert bench(build, "--harness-verbosity", "1", key="h2") == cli.EXIT_OK
    assert "BM_Quiet/1024: stdout, 7 lines (also in" in log_of(tmp, "h1")  # all 7 kept: under the cap
    assert "BM_Quiet/1024: stdout, 7 lines (last 3; all in " in log_of(tmp, "h2")
    assert bench(build, key="h3") == cli.EXIT_OK  # without it the cap is the usual 40: nothing is cut here either
    assert "stdout, 7 lines (also in" in log_of(tmp, "h3")


def test_pyperf_is_given_verbose_and_says_more(tmp):
    pytest.importorskip("pyperf")
    (tmp / "m.py").write_text("def bench_x():\n    sum(range(100))\n")
    for command in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "m"]):
        subprocess.run(["git", *command], cwd=tmp, check=True)
    common = ["bench", "m.py", "--no-ingest", "--processes", "2", "--values", "2", "--warmups", "0", "--loops", "1"]
    assert cli.main([*common, "--run-key", "quiet"]) == cli.EXIT_OK
    assert cli.main([*common, "--run-key", "loud", "--harness-verbosity", "1"]) == cli.EXIT_OK
    quiet = next((tmp / "results" / "quiet").glob("artifacts/*/0000.stdout")).read_text()
    loud = next((tmp / "results" / "loud").glob("artifacts/*/0000.stdout")).read_text()
    assert len(loud.splitlines()) > len(quiet.splitlines()) and "- value 1:" in loud and "- value 1:" not in quiet
    assert " --verbose " in (tmp / "results" / "loud" / "bx.log").read_text()


def test_verbose_in_the_environment_past_two_is_the_environments_and_does_not_pose_as_a_flag(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_VERBOSE", "3")
    assert bench(build) == cli.EXIT_OK
    assert order_of(tmp)["harness_verbosity"] == 1
    assert " DEBUG setting harness verbosity: 1 (environment BENCHX_VERBOSE)" in log_of(tmp)


def test_an_explicit_harness_verbosity_in_the_environment_beats_verbose_in_the_environment(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_VERBOSE", "4")
    monkeypatch.setenv("BENCHX_HARNESS_VERBOSITY", "0")
    assert bench(build) == cli.EXIT_OK
    assert "harness_verbosity" not in order_of(tmp)  # 0: nothing more, though BENCHX_VERBOSE=4 says 2
    assert " DEBUG setting harness verbosity: 0 (environment BENCHX_HARNESS_VERBOSITY)" in log_of(tmp)


def test_the_flags_still_beat_the_environment(build, tmp, monkeypatch):
    monkeypatch.setenv("BENCHX_HARNESS_VERBOSITY", "0")
    assert bench(build, "-vvv") == cli.EXIT_OK
    assert order_of(tmp)["harness_verbosity"] == 1
    assert " DEBUG setting harness verbosity: 1 (flag: -v past -vv)" in log_of(tmp)


def test_config_show_refuses_a_bad_harness_verbosity_with_nothing_half_printed(tmp, monkeypatch, capsys):
    monkeypatch.setenv("BENCHX_HARNESS_VERBOSITY", "loud")
    assert cli.main(["config", "show"]) == cli.EXIT_REFUSED
    seen = capsys.readouterr()
    assert seen.out == "" and "BENCHX_HARNESS_VERBOSITY=loud: not a whole number from 0" in seen.err
