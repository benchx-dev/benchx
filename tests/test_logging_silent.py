"""What the log says about the decisions that used to be silent (logging_plan.md, Phase 3): the
comparison's verdicts and what it dropped, a read-mode comparison in the run's log, the tree's identity,
the store."""

import logging
import os
import re

import pytest

from benchx import cli, compare, snapshot
from benchx.store import Store
from conftest import cases, make_build
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
    assert any(level == "DEBUG" and m.startswith("compare: run run, profile revisions") and
               "40 result document(s) used of 40 offered" in m for level, m in messages)
    units = [m for level, m in messages if level == "INFO" and m.startswith("unit ")]
    assert len(units) == len(doc["units"]) == 4
    assert all(re.search(r"regressed, rounds 5, reference [\d.e-]+, effect \+50\.00%, noise [\d.]+%, k x noise", m)
               and m.endswith("attempts baseline success 5; contender success 5") for m in units)
    assert any(level == "INFO" and m.startswith("compared run run, profile revisions") and "4 unit(s), 4 regressed" in m
               and "0 missing, 0 invariant(s) failed, 0 excluded" in m for level, m in messages)
    assert any(level == "INFO" and m.startswith("baseline ") and "matched by" in m for level, m in messages)


def test_the_document_is_what_it_was_without_the_log(caplog):
    quiet = evaluate(run_of())
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        loud = evaluate(run_of())
    assert quiet == loud  # logging does not touch the comparator's result


def test_a_failed_invariant_and_an_indeterminate_unit_make_the_summary_a_warning(caplog):
    docs = run_of(rounds=2)  # two rounds are the fewest, and min_rounds is 3
    for d in docs:
        if d["coordinates"]["workload"]["parameters"]["dtype"] == "'int16'":  # the sides disagree about a fact
            d["coordinates"]["environment"]["system"]["runtime"] = {"python": "3.13" if d["revision"]["key"] == HEAD
                                                                     else "3.12"}
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate(docs)
    messages = records(caplog)
    assert any(level == "WARNING" and m.startswith("invariant sides-agree failed for " + NAME) and "differ" in m
               for level, m in messages)
    assert any(level == "INFO" and "indeterminate, reason insufficient-rounds, rounds 2" in m and
               "3 paired rounds required" in m for level, m in messages)
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


def test_a_second_success_in_a_round_is_a_warning_naming_what_it_overwrote(caplog):
    docs = run_of(rounds=3)
    parameters = docs[0]["coordinates"]["workload"]["parameters"]
    extras = [case(side, 1, slot, 9.0, parameters) for side, slot in ((BASE, 2), (HEAD, 3))]  # one per side, in
    # the slots the sides already have in round 1: the attempts stay equal and the slots still alternate
    for extra in extras:
        extra["ingest_key"] += ":again"
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        evaluate([*docs, *extras])
    assert any(level == "WARNING" and "second success(es) of a side in one round overwrote the first" in m
               for level, m in records(caplog))


def test_a_min_rounds_below_two_is_said_once(caplog):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        compare.compare(run_of(), run_key="run", profile="revisions", baseline=BASE, min_rounds=1)
    warned = [m for level, m in records(caplog) if level == "WARNING" and m.startswith("min_rounds 1 is below 2")]
    assert len(warned) == 1


# --- a read-mode comparison joins the run's log

@pytest.fixture
def session(repo, tmp, monkeypatch):
    """Three rounds of two builds in one run key, as a loop of `bx bench` calls leaves them."""
    builds = [make_build(tmp / name, repo[tree], cases(), **extra)
              for name, tree, extra in (("b-base", "wt-base", {}), ("b-head", "wt-head", {}))]
    slot = 0
    for round_ in range(3):
        for build in builds:
            assert cli.main(["bench", str(build), *GBENCH, "--run-key", "S", "--round", str(round_),
                             "--slot", str(slot)]) == cli.EXIT_OK
            slot += 1
    return repo["base"][:10]


def test_compare_run_appends_the_comparison_to_the_run_folders_log(session, tmp, capsys):
    capsys.readouterr()
    assert cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session]) == cli.EXIT_OK
    out = capsys.readouterr()
    text = (tmp / "results" / "S" / "bx.log").read_text()
    reading = text[text.rindex(" INFO start: "):]
    assert "bx compare --run S" in reading.splitlines()[0]
    assert " INFO compared run S, profile revisions" in reading and " INFO unit " in reading
    assert reading.splitlines()[-1].endswith(" s") and " INFO end: ok (exit 0), " in reading.splitlines()[-1]
    assert not any(re.match(r"^\d{4}-\d\d-", line) for line in out.err.splitlines())  # stderr stays as it was


def test_compare_run_without_a_run_folder_leaves_no_log(session, tmp):
    assert cli.main(["compare", "--run", "nosuch", "--profile", "revisions", "--baseline", session]) == cli.EXIT_REFUSED
    assert not (tmp / "results" / "nosuch").exists()


def test_compare_run_with_no_log_appends_nothing(session, tmp):
    before = (tmp / "results" / "S" / "bx.log").read_text()
    assert cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session, "--no-log"]) == cli.EXIT_OK
    assert (tmp / "results" / "S" / "bx.log").read_text() == before


def test_the_read_does_not_need_to_write_beside_it(session, tmp):
    log = tmp / "results" / "S" / "bx.log"
    log.chmod(0o444)
    try:
        assert cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session]) == cli.EXIT_OK  # skipped, silently
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


def test_a_setup_file_that_is_missing_or_unreadable_is_a_warning_that_names_the_variable_not_the_path(tmp, caplog):
    snapshot.log.configure(False)
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        assert snapshot.setup_facts(tmp, {"BENCHX_SETUP_FILE": str(tmp / "secret-place.json")}) == (None, None)
        broken = tmp / "setup.json"
        broken.write_text("[1, 2]")
        assert snapshot.setup_facts(tmp, {"BENCHX_SETUP_FILE": str(broken)}) == (None, "collector-failed")
    messages = [m for level, m in records(caplog) if level == "WARNING"]
    assert any(m.startswith("BENCHX_SETUP_FILE names a file that does not exist") for m in messages)
    assert any("the file named by BENCHX_SETUP_FILE" in m and "not an object" in m for m in messages)
    assert not any("secret-place" in m or str(broken) in m for m in messages)  # rule 3: not the value


def test_the_store_says_what_it_swept_what_it_refused_and_what_it_read(session, tmp, caplog, capsys):
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        # the same keys again: the fake binary draws fresh noise, so every payload differs
        assert cli.main(["bench", str(tmp / "b-base"), *GBENCH, "--run-key", "S", "--round", "0", "--slot", "0"]) \
            == cli.EXIT_INVALID
    messages = records(caplog)
    assert any(level == "WARNING" and "idempotency-conflict: " in m and "already holds payload" in m
               for level, m in messages)
    assert any(level == "DEBUG" and m.startswith("swept 5 file(s): 4 result document(s), 1 work order(s)")
               for level, m in messages)
    assert any(level == "DEBUG" and "ingest: 0 ingested, 0 duplicate, 4 rejected" in m for level, m in messages)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        Store(tmp / "store.parquet").documents("S")
    assert any("read " in m and " row(s), provenance, document" in m for _, m in records(caplog))
    assert any(m.startswith("documents(S): ") for _, m in records(caplog))
    with caplog.at_level(logging.DEBUG, logger="benchx"):
        assert not Store(tmp / "absent.parquet").documents("S")
    assert any("absent, read as an empty store" in m for _, m in records(caplog))


@pytest.mark.parametrize("extra", [["--tables"], ["--tables", "--all"]])
def test_compare_tables_keeps_stdout_clean_with_the_log_on(session, capsys, extra):
    capsys.readouterr()
    assert cli.main(["compare", "--run", "S", "--profile", "revisions", "--baseline", session, *extra, "--log"]) \
        == cli.EXIT_OK
    out = capsys.readouterr()
    assert "Before" in out.out or "Change" in out.out or out.out.strip()  # the tables, whatever they hold
    assert not any(re.match(r"^\d{4}-\d\d-\d\dT", line) for line in out.out.splitlines())
    assert " INFO compared run S" in out.err


def test_a_store_that_cannot_be_written_says_where_the_results_are(session, tmp, monkeypatch, capsys):
    from benchx import store as store_mod
    monkeypatch.setattr(store_mod.pq, "write_table", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left")))
    assert cli.main(["bench", str(tmp / "b-base"), *GBENCH, "--run-key", "fresh"]) == 1  # a crash: the status Python gives
    text = (tmp / "results" / "fresh" / "bx.log").read_text()
    assert " ERROR cannot write " in text and "No space left" in text
    assert re.search(r" ERROR the order: the results are in \S+/results/fresh, not delivered to the store; "
                     r"bx ingest \S+/results/fresh stores them", text)
    assert " INFO end: crashed (exit 1), " in text
