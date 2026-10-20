"""The session loop and the two modes of `bx compare` (prototype-design.md §1, §5)."""

import json

import pytest

from benchx import cli, runner, session
from benchx.store import Store
from conftest import NOISY, QUIET, cases, make_build


@pytest.fixture
def builds(repo, tmp):
    base = make_build(tmp / "build-base", repo["wt-base"], cases())
    head = make_build(tmp / "build-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))
    return {"baseline": f"{base}:{repo['wt-base']}", "contender": f"{head}:{repo['wt-head']}"}


def verdicts(doc):
    return {u["workload"]["name"]: u["verdict"] for u in doc["units"]}


def test_session_alternates_delivers_and_compares(repo, tmp, builds):
    store = Store(tmp / "store.parquet")
    doc = session.compare_targets(builds["baseline"], builds["contender"], profile="revisions",
                                  adapter="google-benchmark", suite="demo-bench", rounds=6, store=store, run_key="s1", out=tmp / "out")
    assert doc["failed_invariants"] == [] and doc["missing"] == [] and doc["local_only"] is False
    assert verdicts(doc) == {QUIET: "regressed", NOISY: "no-change-detected"}
    by_slot = {}
    for d in store.documents("s1"):
        by_slot.setdefault(d["procedure"]["slot"], []).append(d)
    assert sorted(by_slot) == list(range(12))  # 6 rounds x 2 sides, one order each
    assert all(len(docs) == 4 for docs in by_slot.values())  # per case, a result and its load average
    trees = [{d["provenance"]["subject_tree"] for d in by_slot[slot]} for slot in sorted(by_slot)]
    assert all(len(t) == 1 for t in trees)
    assert all(a != b for a, b in zip(trees, trees[1:]))  # every slot switches sides
    assert all(d["procedure"]["round"] == slot // 2 for slot, docs in by_slot.items() for d in docs)


def test_session_environments(repo, tmp):
    plain = make_build(tmp / "plain", repo["wt-head"], cases())
    hardened = make_build(tmp / "hardened", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3), hardened=True)
    doc = session.compare_targets(f"{plain}:{repo['wt-head']}", f"{hardened}:{repo['wt-head']}",
                                  profile="environments", adapter="google-benchmark", suite="demo-bench", rounds=6,
                                  label=("build", "plain", "hardened"), project="demo",
                                  store=Store(tmp / "store.parquet"), run_key="e1", out=tmp / "out")
    assert doc["varying"] == "provenance.labels.build" and doc["failed_invariants"] == []
    assert verdicts(doc) == {QUIET: "regressed", NOISY: "no-change-detected"}


def test_dirty_tree_refused_before_anything_runs(repo, tmp, builds):
    (repo["wt-head"] / "bench.cpp").write_text("// uncommitted\n")
    store = Store(tmp / "store.parquet")
    with pytest.raises(session.SessionError, match="clean trees"):
        session.compare_targets(builds["baseline"], builds["contender"], profile="revisions",
                                adapter="google-benchmark", suite="demo-bench", rounds=4, store=store, run_key="d1", out=tmp / "out")
    assert not (tmp / "out").exists() and not store.path.exists()


@pytest.mark.parametrize("kwargs, message", [
    ({"profile": "environments"}, "needs --label"),
    ({"profile": "revisions", "label": ("build", "a", "b")}, "--label names the sides"),
    ({"profile": "revisions", "rounds": 0}, "at least 1"),
    ({"profile": "revisions", "suite": "missing-binary"}, "suite binary not found"),
    ({"profile": "history"}, "not in the prototype"),
])
def test_session_refusals(tmp, builds, kwargs, message):
    args = {"adapter": "google-benchmark", "suite": "demo-bench", "rounds": 3, "store": Store(tmp / "store.parquet"), "out": tmp / "out", **kwargs}
    with pytest.raises(session.SessionError, match=message):
        session.compare_targets(builds["baseline"], builds["contender"], **args)


def test_a_refused_order_stops_the_session(tmp, builds, monkeypatch):
    def refuse(*_, **__):
        raise runner.Refused("deferred")
    monkeypatch.setattr(runner, "run", refuse)
    with pytest.raises(runner.Refused):
        session.compare_targets(builds["baseline"], builds["contender"], profile="revisions",
                                adapter="google-benchmark", suite="demo-bench", rounds=3, store=Store(tmp / "store.parquet"), out=tmp / "out")


def test_measure_mode_equals_read_mode(repo, tmp, builds, capsys):
    common = ["--profile", "revisions"]
    assert cli.main(["compare", builds["baseline"], builds["contender"], *common, "--adapter", "google-benchmark", "--suite", "demo-bench",
                     "--rounds", "5", "--run-key", "cli-1", "--out", str(tmp / "o"), "--json"]) == cli.EXIT_OK
    measured = json.loads(capsys.readouterr().out)
    assert cli.main(["compare", "--run", "cli-1", *common, "--baseline", repo["base"][:10], "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == measured
    assert measured["mode"] == "run" and verdicts(measured)[QUIET] == "regressed"
    # criterion 8: the output directory, orders subdirectory and all, compares to the same document
    assert cli.main(["compare", "--run", "cli-1", *common, "--baseline", repo["base"][:10], "--json",
                     "--results", str(tmp / "o")]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == measured
    # re-ingesting what measure mode already delivered is a visible no-op (criterion 2)
    assert cli.main(["ingest", str(tmp / "o")]) == cli.EXIT_OK
    assert "ingested 0" in capsys.readouterr().out


@pytest.mark.parametrize("argv, message", [
    (["--profile", "revisions"], "give two targets"),
    (["A", "B", "--run", "k", "--profile", "revisions"], "not both"),
    (["A", "--profile", "revisions"], "exactly two targets"),
    (["A", "B", "--profile", "revisions", "--rounds", "3", "--adapter", "pyperf"], "--suite is required"),
    (["A", "B", "--profile", "revisions", "--suite", "s", "--adapter", "pyperf"], "--rounds is required"),
    (["A", "B", "--profile", "revisions", "--suite", "s", "--rounds", "3", "--baseline", "x"], "read mode"),
    (["--run", "k", "--profile", "revisions"], "needs --baseline"),
    (["--run", "k", "--profile", "revisions", "--baseline", "x", "--rounds", "3"], "measure mode"),
])
def test_mode_selection(argv, message, capsys):
    assert cli.main(["compare", *argv]) == cli.EXIT_REFUSED
    assert message in capsys.readouterr().err
