"""What the log says about the decisions that used to be silent: the
comparison's verdicts and what it dropped, a read-mode comparison in the run's log, the tree's identity,
the store."""

import logging
import re
from pathlib import Path

import pytest

from benchx import cli, compare, config, runner, snapshot
from benchx import session as session_module  # `session` is a fixture here
from benchx.store import Store
from conftest import HAS_PYARROW, cases, make_build
from test_comparator import BASE, HEAD
from test_parameter_order import NAME, case, evaluate, run_of

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]


def records(caplog, level=logging.DEBUG):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= level]


# --- the comparison


def test_a_comparison_logs_its_inputs_each_unit_and_a_summary(caplog):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        doc = evaluate(run_of())
    messages = records(caplog)
    assert any(
        level == "DEBUG"
        and m.startswith("compare: run run, profile revisions")
        and "40 result document(s) used of 40 offered" in m
        for level, m in messages
    )
    units = [m for level, m in messages if level == "INFO" and m.startswith("unit ")]
    assert len(units) == len(doc["units"]) == 4
    assert all(
        re.search(
            r"regressed, rounds 5, reference [\d.e-]+, effect \+50\.00%, noise [\d.]+%, threshold [\d.]+% \([\d.]+ x noise\)",
            m,
        )
        and m.endswith("attempts baseline success 5; contender success 5")
        for m in units
    )
    assert any(
        level == "INFO"
        and m.startswith("compared run run, profile revisions")
        and "4 unit(s), 4 regressed" in m
        and "0 missing, 0 invariant(s) failed, 0 excluded" in m
        for level, m in messages
    )
    assert any(level == "INFO" and m.startswith("baseline ") and "matched by" in m for level, m in messages)


def test_the_document_is_what_it_was_without_the_log(caplog):
    quiet = evaluate(run_of())
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        loud = evaluate(run_of())
    assert quiet == loud  # logging does not touch the comparator's result


def test_a_failed_invariant_and_an_indeterminate_unit_make_the_summary_a_warning(caplog):
    docs = run_of(rounds=2)  # two rounds are the fewest, and min_rounds is 4
    for d in docs:
        if d["coordinates"]["workload"]["parameters"]["dtype"] == "'int16'":  # the sides disagree about a fact
            d["coordinates"]["environment"]["system"]["runtime"] = {
                "python": "3.13" if d["revision"]["key"] == HEAD else "3.12"
            }
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate(docs)
    messages = records(caplog)
    assert any(
        level == "WARNING" and m.startswith("invariant sides-agree failed for " + NAME) and "differ" in m
        for level, m in messages
    )
    assert any(
        level == "INFO"
        and "indeterminate, reason insufficient-rounds, rounds 2" in m
        and "4 paired rounds required" in m
        for level, m in messages
    )
    summary = [(level, m) for level, m in messages if m.startswith("compared run")]
    assert summary[0][0] == "WARNING" and "indeterminate: insufficient-rounds 2" in summary[0][1]


def test_an_edited_benchmark_is_logged_as_not_comparable_and_makes_the_summary_a_warning(caplog):
    """The benchmark's version differs between the sides: not a failed invariant, a unit with no pair."""
    docs = run_of(rounds=3)
    for d in docs:
        if d["coordinates"]["workload"]["parameters"]["dtype"] == "'int16'":
            d["coordinates"]["protocol"]["asv"] = {"benchmark_version": "v2" if d["revision"]["key"] == HEAD else "v1"}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate(docs)
    messages = records(caplog)
    assert not any(m.startswith("invariant ") for level, m in messages)  # no failed invariant, only a unit
    assert any(level == "INFO" and "indeterminate, reason benchmark-changed, rounds 0" in m for level, m in messages)
    summary = [(level, m) for level, m in messages if m.startswith("compared run")]
    assert summary[0][0] == "WARNING" and "indeterminate: benchmark-changed" in summary[0][1]


def test_a_second_success_in_a_round_is_refused_and_the_warning_names_the_unit(caplog):
    """The comparator never picks one of two successes of a side in one round (`duplicate-points`):
    the unit is refused, and the log names it, as for any failed invariant of one unit."""
    docs = run_of(rounds=3)
    parameters = docs[0]["coordinates"]["workload"]["parameters"]
    extras = [case(side, 1, slot, 9.0, parameters) for side, slot in ((BASE, 2), (HEAD, 3))]  # one per side, in
    # the slots the sides already have in round 1: the attempts stay equal and the slots still alternate
    for extra in extras:
        extra["ingest_key"] += ":again"
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate([*docs, *extras])
    assert any(
        level == "WARNING" and m.startswith("invariant duplicate-points failed for " + NAME)
        for level, m in records(caplog)
    )


def test_a_min_rounds_below_two_is_said_once(caplog):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        compare.compare(run_of(), run_key="run", profile="revisions", baseline=BASE, min_rounds=1)
    warned = [m for level, m in records(caplog) if level == "WARNING" and m.startswith("min_rounds 1 is below 2")]
    assert len(warned) == 1


# --- a read-mode comparison joins the run's log


@pytest.fixture
def session(repo, tmp, monkeypatch):
    """Four rounds of two builds in one run key, as a loop of `bx bench` calls leaves them."""
    builds = [
        make_build(tmp / name, repo[tree], cases(), **extra)
        for name, tree, extra in (("b-base", "wt-base", {}), ("b-head", "wt-head", {}))
    ]
    slot = 0
    for round_ in range(4):
        for build in builds:
            assert (
                cli.main(["bench", str(build), *GBENCH, "--run-key", "S", "--round", str(round_), "--slot", str(slot)])
                == cli.EXIT_OK
            )
            slot += 1
    return repo["base"][:10]


def test_compare_run_appends_the_comparison_to_the_run_folders_log(session, tmp, capsys):
    capsys.readouterr()
    assert cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session]) == cli.EXIT_OK
    out = capsys.readouterr()
    text = (tmp / "results" / "S" / "bx.log").read_text()
    reading = text[text.rindex(" INFO start: ") :]
    assert "bx compare --run S" in reading.splitlines()[0]
    assert " INFO compared run S, profile revisions" in reading and " INFO unit " in reading
    assert reading.splitlines()[-1].endswith(" s") and " INFO end: ok (exit 0), " in reading.splitlines()[-1]
    assert not any(re.match(r"^\d{4}-\d\d-", line) for line in out.err.splitlines())  # stderr stays as it was


def test_compare_run_without_a_run_folder_leaves_no_log(session, tmp):
    assert cli.main(["compare", "--run", "nosuch", "--profile", "revisions", "--baseline", session]) == cli.EXIT_REFUSED
    assert not (tmp / "results" / "nosuch").exists()


def test_compare_run_with_no_log_appends_nothing(session, tmp):
    before = (tmp / "results" / "S" / "bx.log").read_text()
    assert (
        cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session, "--no-log"]) == cli.EXIT_OK
    )
    assert (tmp / "results" / "S" / "bx.log").read_text() == before


def test_the_read_does_not_need_to_write_beside_it(session, tmp):
    log = tmp / "results" / "S" / "bx.log"
    log.chmod(0o444)
    try:
        assert (
            cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session]) == cli.EXIT_OK
        )  # skipped, silently
    finally:
        log.chmod(0o644)


# --- the tree, the setup file, the store


def test_a_file_that_cannot_be_read_is_a_warning_naming_it(tmp, caplog):
    tree = tmp / "tree"
    tree.mkdir()
    (tree / "a.py").write_text("x\n")
    hidden = tree / "hidden.txt"
    hidden.write_text("y\n")
    hidden.chmod(0)
    try:
        with caplog.at_level(logging.DEBUG, logger="benchx"):
            assert snapshot.directory_identity(tree) is None
    finally:
        hidden.chmod(0o644)
    assert any(level == "WARNING" and str(hidden) in m and "cannot be identified" in m for level, m in records(caplog))


def test_a_setup_file_that_is_missing_or_unreadable_is_a_warning_that_names_the_variable_and_the_path(tmp, caplog):
    snapshot.log.configure(False)
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        assert snapshot.setup_facts(tmp, {"BENCHX_SETUP_FILE": str(tmp / "secret-place.json")}) == (None, None)
        broken = tmp / "setup.json"
        broken.write_text("[1, 2]")
        assert snapshot.setup_facts(tmp, {"BENCHX_SETUP_FILE": str(broken)}) == (None, "collector-failed")
    messages = [m for level, m in records(caplog) if level == "WARNING"]
    # BENCHX_SETUP_FILE is a benchx control variable: its value is logged here as cli._log_environment logs it
    assert any(
        m.startswith("BENCHX_SETUP_FILE names a file that does not exist") and "secret-place" in m for m in messages
    )
    assert any(str(broken) in m and "not an object" in m for m in messages)


def test_the_store_says_what_it_swept_what_it_refused_and_what_it_read(session, tmp, caplog, capsys):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        # the same keys again: the fake binary draws fresh noise, so every payload differs
        assert (
            cli.main(["bench", str(tmp / "b-base"), *GBENCH, "--run-key", "S", "--round", "0", "--slot", "0"])
            == cli.EXIT_INVALID
        )
    messages = records(caplog)
    assert any(
        level == "WARNING" and "idempotency-conflict: " in m and "already holds payload" in m for level, m in messages
    )
    assert any(
        level == "DEBUG" and m.startswith("swept 5 file(s): 4 result document(s), 1 work order(s)")
        for level, m in messages
    )
    assert any(level == "DEBUG" and "ingest: 0 ingested, 0 duplicate, 4 rejected" in m for level, m in messages)
    caplog.clear()
    store_path = config.load(tmp).store_path
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        Store(store_path).documents("S")
    read_needle = " row(s), provenance, document" if HAS_PYARROW else " row(s)"
    assert any("read " in m and read_needle in m for _, m in records(caplog))
    assert any(m.startswith("documents(S): ") for _, m in records(caplog))
    absent_path = tmp / ("absent.parquet" if HAS_PYARROW else "absent.jsonl")
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        assert not Store(absent_path).documents("S")
    assert any("absent, read as an empty store" in m for _, m in records(caplog))


@pytest.mark.parametrize("extra", [["--tables"], ["--tables", "--all"]])
def test_compare_tables_keeps_stdout_clean_with_the_log_on(session, capsys, extra):
    capsys.readouterr()
    assert (
        cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session, *extra, "--log"])
        == cli.EXIT_OK
    )
    out = capsys.readouterr()
    assert "Before" in out.out or "Change" in out.out or out.out.strip()  # the tables, whatever they hold
    assert not any(re.match(r"^\d{4}-\d\d-\d\dT", line) for line in out.out.splitlines())
    assert " INFO compared run S" in out.err


def test_a_store_that_cannot_be_written_says_where_the_results_are(session, tmp, monkeypatch, capsys):
    from benchx import store as store_mod

    monkeypatch.setattr(store_mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    assert (
        cli.main(["bench", str(tmp / "b-base"), *GBENCH, "--run-key", "fresh"]) == 1
    )  # a crash: the status Python gives
    text = (tmp / "results" / "fresh" / "bx.log").read_text()
    assert " ERROR cannot write " in text and "No space left" in text
    assert re.search(
        r" ERROR the order: the results are in \S+/results/fresh, not delivered to the store; "
        r"bx ingest \S+/results/fresh stores them",
        text,
    )
    assert " INFO end: crashed (exit 1), " in text


def test_a_side_whose_results_disagree_about_their_declarations_is_a_warning_naming_the_field(caplog):
    docs = run_of(rounds=3)
    for d in docs[:4]:
        d["provenance"]["info"] = {"how_built": "cmake -B build"}  # the first results of the baseline say how
    for d in docs[4:]:
        d["provenance"]["info"] = {"how_built": "meson setup build"}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate(docs)
    messages = records(caplog)
    assert any(
        level == "DEBUG" and m.startswith("the baseline side's declarations come from ") for level, m in messages
    )
    assert any(level == "WARNING" and "side's results disagree about how_built" in m for level, m in messages)
    assert not any("cmake -B build" in m or "meson setup" in m for _, m in messages)  # the field's name, not its text


def test_history_says_when_nothing_matches_and_when_several_series_do(session, caplog):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        assert cli.main(["history", "ffffffffffff"]) == cli.EXIT_OK
        assert cli.main(["history", ""]) == cli.EXIT_OK  # an empty prefix matches every series
    messages = records(caplog)
    assert any(level == "INFO" and m == "no series matches ffffffffffff" for level, m in messages)
    assert any(
        level == "WARNING" and " matches " in m and "series, whose points are listed together" in m
        for level, m in messages
    )


def test_validate_logs_its_kind_and_each_valid_file(tmp):
    from conftest import EXAMPLES

    example = next(EXAMPLES.glob("*.json"))
    log_file = tmp / "v.log"
    assert cli.main(["validate", str(example), "--log-file", str(log_file)]) == cli.EXIT_OK
    text = log_file.read_text()
    assert " INFO validating 1 file(s) as measurement-result" in text
    assert f" DEBUG {example} is a valid measurement-result" in text


# --- the rest of §5's rows


def test_series_and_head_say_when_the_store_has_nothing_to_list(tmp):
    log_file = tmp / "empty.log"
    assert cli.main(["series", "--log-file", str(log_file)]) == cli.EXIT_OK
    assert cli.main(["head", "--log-file", str(log_file)]) == cli.EXIT_OK
    text = log_file.read_text()
    assert " INFO no series in the store" in text and " holds no result" in text


def test_the_tables_say_how_many_rows_they_showed_and_what_they_hid(session, tmp):
    log_file = tmp / "t.log"
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "S",
                "--profile",
                "revisions",
                "--baseline",
                session,
                "--tables",
                "--log-file",
                str(log_file),
            ]
        )
        == cli.EXIT_OK
    )
    assert re.search(r" INFO --tables: \d+ row\(s\) shown; hidden by verdict: ", log_file.read_text())


def test_a_schema_error_is_logged_with_its_path_and_keyword(tmp):
    bad = tmp / "bad.json"
    bad.write_text('{"schema_version": "benchx/measurement-result/0.1.0", "surprise": 1}')
    log_file = tmp / "v.log"
    assert cli.main(["validate", str(bad), "--log-file", str(log_file)]) == cli.EXIT_INVALID
    assert re.search(r" DEBUG schema error at /[^:]*: \w+: ", log_file.read_text())


def test_the_asv_runtime_returns_what_the_suite_switched_off(tmp):
    from benchx.adapters import asv_runtime

    suite = tmp / "benchmarks"
    suite.mkdir()
    (suite / "__init__.py").write_text("")
    (suite / "bench.py").write_text(
        "def time_on():\n    pass\n\n"
        "def time_off():\n    pass\n\n"
        "time_off.skip_benchmark = True\n\n"
        "class Suite:\n    def time_method(self):\n        pass\n    def time_hidden(self):\n        pass\n"
        "    time_hidden.skip_benchmark = True\n"
    )
    found, failed, left_out = asv_runtime.discover(str(suite))
    assert failed == [] and sorted(b.name for b in found) == ["bench.Suite.time_method", "bench.time_on"]
    assert sorted(left_out) == ["benchmarks.bench.Suite.time_hidden", "benchmarks.bench.time_off"]


def test_a_plan_says_where_each_setting_came_from_and_what_the_order_asked_for_in_vain(caplog):
    from benchx.adapters import asv

    info = {"declared": {"setup_scope": "call", "repeat": 0}, "has_setup": True}
    invocation = {"processes": 20, "values": None, "loops": None, "min_time": 0.2, "warmups": None}
    plan = asv._plan(info, invocation)
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        asv._note_plan(info, invocation, plan)
    messages = [m for _, m in records(caplog)]
    assert any(m.startswith("plan: values 5 (default), loops 1 (setup_scope), min_time 0.2 (order)") for m in messages)
    assert any(
        "the order's min_time 0.2 s is lost: the loops are fixed at 1 by the default setup_scope" in m for m in messages
    )
    assert any("repeat 0 is not a form" in m and "the default 5 values are used" in m for m in messages)


def test_a_partial_case_line_gives_completed_over_attempted_and_a_warning_its_reason():
    from benchx import runner

    document = {
        "provenance": {"info": {}},
        "measurement": {"status": "partial"},
        "procedure": {"completed_repetitions": 3, "attempted_repetitions": 5},
    }
    level, text, bad = runner._case_line(
        [("partial", "harness.partial", document)],
        ["env-mismatch"],
        {"timed_out": False, "exit_status": 0},
        1.0,
        0.5,
        {"env-mismatch": "2 of 3 worker(s) had the environment the manager passed"},
    )
    assert text.startswith(
        "partial (harness.partial) 3/5 [env-mismatch: 2 of 3 worker(s) had the environment the "
        "manager passed], exit 0, 1.0 s, load 0.50"
    ) and (level, bad) == (logging.WARNING, True)


def test_the_overlap_of_two_sets_of_facts_names_paths_never_values():
    from benchx import runner

    base = {"os": {"family": "Linux", "kernel": "6"}, "process": {"environment": {"A": "1"}}}
    facts = {"os": {"family": "Linux"}, "process": {"environment": {"A": "2"}}, "cpu": {"x": 1}}
    assert runner._overlap(base, facts) == ["/process/environment/A"]


# --- review: what is the same in every order is INFO once per distinct thing, not once per command


def identity(path, dirty="dirty"):
    return {"revision": "a" * 40, "tree": "t" * 40, "dirty": dirty, "changes": ["M a.py"], "path": path}


def test_each_distinct_interpreter_and_dirty_tree_is_info_once_and_the_repeats_are_debug(caplog):
    snapshot.log.configure(False)  # a command's state: nothing spent yet
    sides = [("/py/base", "/src/base"), ("/py/head", "/src/head")]
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        for python, path in sides * 3:  # three rounds of a comparison whose sides differ in both
            runner._log_identity({"python": python}, identity(path), None, Path(path))
    messages = records(caplog)
    ran = [(level, m) for level, m in messages if m.startswith("the benchmarks run under")]
    dirty = [(level, m) for level, m in messages if m.startswith("the subject tree is dirty")]
    assert [level for level, _ in ran] == ["INFO", "INFO"] + ["DEBUG"] * 4
    assert [m.rsplit(" ", 1)[1] for _, m in ran[:2]] == ["/py/base", "/py/head"]
    assert [level for level, _ in dirty] == ["INFO", "INFO"] + ["DEBUG"] * 4


def test_a_dirty_benchmark_in_git_is_not_said_to_be_outside_git(caplog):
    snapshot.log.configure(False)
    suite = {"uri": "file:///suite", "type": "git", "revision": "b" * 40, "tree": "t" * 40, "dirty": "dirty"}
    with caplog.at_level(logging.INFO, logger="benchx"):
        runner._log_identity({}, identity("/s", "clean"), suite, "/s")
        runner._log_identity({}, identity("/s", "clean"), {**suite, "type": "directory", "uri": "file:///other"}, "/s")
    said = [m for level, m in records(caplog) if m.startswith("the benchmark tree is dirty")]
    assert len(said) == 2
    assert "not under git" not in said[0] and said[0].startswith("the benchmark tree is dirty; every result carries")
    assert "(not under git: a directory of files is always dirty)" in said[1]


def test_a_side_with_one_noisy_condition_does_not_spend_the_warning_of_a_side_with_the_other(repo, tmp, caplog):
    scaling = make_build(tmp / "scaling", repo["wt-base"], cases(), scaling=True)
    debug = make_build(tmp / "debug", repo["wt-head"], cases(), build_type="debug")
    store = Store(tmp / ("store.parquet" if HAS_PYARROW else "store.jsonl"))
    with caplog.at_level(logging.WARNING, logger="benchx"):
        session_module.compare_targets(
            f"{scaling}:{repo['wt-base']}",
            f"{debug}:{repo['wt-head']}",
            profile="revisions",
            adapter="google-benchmark",
            suite="demo-bench",
            rounds=2,
            store=store,
            run_key="n1",
            out=tmp / "out",
        )
    said = [m for level, m in records(caplog, logging.WARNING) if "the harness's own context says" in m]
    assert len(said) == 2  # once for each condition, however many orders and cases
    assert sum("CPU frequency scaling is enabled" in m for m in said) == 1
    assert sum("the benchmark library is a debug build" in m for m in said) == 1


# --- review: the summary of a comparison


def test_a_missing_unit_makes_the_summary_a_warning(caplog):
    docs = [
        d
        for d in run_of(rounds=3)
        if not (d["revision"]["key"] == HEAD and d["coordinates"]["workload"]["parameters"]["dtype"] == "'int16'")
    ]
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        doc = evaluate(docs)
    assert doc["missing"] and not doc["failed_invariants"] and not doc["exclusions"]
    summary = [(level, m) for level, m in records(caplog) if m.startswith("compared run")]
    assert summary[0][0] == "WARNING" and re.search(r"; [1-9]\d* missing, 0 invariant", summary[0][1])


def test_the_names_of_the_sides_are_cut_for_revisions_and_whole_for_anything_else(caplog):
    docs = run_of()
    doc = evaluate(docs)
    doc["sides"]["baseline"], doc["sides"]["contender"] = "numpy-2.1.0-openblas", "numpy-2.1.0-mkl"
    for profile, baseline, contender in (
        ("revisions", "numpy-2.1.0-", "numpy-2.1.0-"),
        ("environments", "numpy-2.1.0-openblas", "numpy-2.1.0-mkl"),
    ):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="benchx"):
            doc["selector"]["params"]["profile"] = profile
            compare._log_outcome(
                doc, docs, compare.methods.get(compare.methods.DEFAULT), doc["method"]["configuration"], asked=BASE
            )
        summary = next(m for level, m in records(caplog) if m.startswith("compared run"))
        assert f"baseline {baseline}, contender {contender}:" in summary
