"""bx compare over either harness: --adapter, its precision flags, and -e.

The refusals and the orders are checked without running a harness. The last
test runs a real pyperf comparison of two revisions, end to end, and is
skipped where pyperf is not installed.
"""

import json
import textwrap
from pathlib import Path

import pytest

from benchx import cli, core, harnesses, session
from benchx.store import Store
from conftest import SOURCE_URI, git

MEASURE = ["A", "B", "--profile", "revisions", "--suite", "s", "--rounds", "1", "--adapter", "pyperf"]


@pytest.mark.parametrize("argv, message", [
    (MEASURE + ["--adapter", "google-benchmark", "--processes", "2"], "google-benchmark takes no --processes"),
    (MEASURE + ["--adapter", "pyperf", "--repetitions", "3"], "pyperf takes no --repetitions"),
    (MEASURE + ["--repetitions", "3"], "pyperf takes no --repetitions"),
    (["A", "B", "--profile", "revisions", "--suite", "s", "--rounds", "1"], "--adapter is required"),
    (["A", "B", "--profile", "revisions", "--rounds", "1", "--adapter", "pyperf"], "--suite is required"),
    (["A", "B", "--profile", "revisions", "--suite", "s", "--adapter", "pyperf"], "--rounds is required"),
    (["A", "B", "--suite", "s", "--rounds", "1", "--adapter", "pyperf"], "--profile is required"),
    (MEASURE + ["--project", "p", "--no-project"], "--project or --no-project"),
    (MEASURE + ["--quantity", "wall-time,wall-time"], "twice"),
    (MEASURE + ["-e", "NOEQUALS"], "-e wants NAME=VALUE"),
    (["--run", "k", "--profile", "revisions", "--baseline", "x", "--adapter", "pyperf"], "belongs to measure mode"),
    (["--run", "k", "--profile", "revisions", "--baseline", "x", "-e", "A=1"], "belongs to measure mode"),
])
def test_flags_that_do_not_fit_are_refused(argv, message, capsys):
    assert cli.main(["compare", *argv]) == cli.EXIT_REFUSED
    assert message in capsys.readouterr().err


def test_a_harness_refusal_comes_before_any_side_is_resolved(tmp):
    # "nowhere" does not exist: the flag is refused first, so that is never looked at.
    with pytest.raises(session.SessionError, match="pyperf takes no --repetitions"):
        session.compare_targets("nowhere", "nowhere", profile="revisions", suite="s", rounds=1,
                                store=Store(tmp / "s.parquet"), adapter="pyperf", flags={"repetitions": 3})


def test_a_pyperf_side_order(tmp):
    side = {"kind": "working_tree", "path": tmp, "source": str(tmp), "identity": {}, "uri": None,
            "text": {"activation": "source .venv/bin/activate"}, "labels": {}}
    pyperf = harnesses.PYPERF
    precision = pyperf.precision(pyperf.flags_to_precision(processes=2, values=3, loops=10))
    order = session._order(side, harness=pyperf, suite="bench/mod.py", filter=harnesses.exact_filter(["f"]),
                           quantities=["wall-time"], precision=precision,
                           environment_variables={"OPENBLAS_NUM_THREADS": "1"}, project=None, run_key="rk",
                           source_uri=SOURCE_URI, round_=0, slot=0, labels=None)
    assert core.check(order, "work-order") == []
    assert order["suites"] == [{"adapter": "pyperf", "suite": "bench/mod.py", "filter": "^(f)$"}]
    assert order["target"]["kind"] == "working_tree"
    assert order["target"]["activation"] == "source .venv/bin/activate"
    assert order["precision"]["repetitions"]["levels"] == [{"unit": "process", "n": 2}, {"unit": "value", "n": 3}]
    assert order["precision"]["calibration"] == {"mode": "fixed", "n_iterations": 10}
    assert order["environment_variables"] == {"OPENBLAS_NUM_THREADS": "1"}


# --- end to end, with real pyperf --------------------------------------------------

MODULE = textwrap.dedent('''
    def f():
        sum(range({work}))
''')


@pytest.fixture
def worktrees(tmp_path):
    """Two commits of a pure-Python suite, a clean worktree at each."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    shas = []
    for work in (200, 2000):
        (src / "mod.py").write_text(MODULE.format(work=work))
        git(src, "add", "-A")
        git(src, "commit", "-qm", f"work {work}")
        shas.append(git(src, "rev-parse", "HEAD"))
    trees = {}
    for name, sha in zip(("base", "head"), shas):
        git(src, "worktree", "add", "-q", "--detach", str(tmp_path / name), sha)
        trees[name] = tmp_path / name
    return trees


def test_pyperf_revisions_end_to_end(tmp, worktrees, monkeypatch):
    pytest.importorskip("pyperf")

    def provider(spec, operation, provider=None):  # what a minimal provider for two worktrees would answer
        tree = worktrees[spec.removeprefix("@")]
        return {"schema_version": "benchx/target-description/0.1.0",
                "target": {"kind": "working_tree", "path": str(tree), "source": {"uri": SOURCE_URI, "type": "git"}},
                "how_built": f"git worktree add {tree}"}

    monkeypatch.setattr(session.target_mod, "resolve", provider)
    doc = session.compare_targets(
        "@base", "@head", profile="revisions", suite="mod.py", rounds=3, store=Store(tmp / "s.parquet"),
        adapter="pyperf", flags={"processes": 2, "values": 2, "loops": 50, "warmups": 1},
        filter=harnesses.exact_filter(["f"]),  # the default case set is bench_*/benchmark_* functions
        environment_variables={"OPENBLAS_NUM_THREADS": "1"}, out=tmp / "out", run_key="py")
    assert [u["workload"]["name"] for u in doc["units"]] == ["f"]
    assert doc["units"][0]["workload"]["parameters"] == {"OPENBLAS_NUM_THREADS": "1"}
    order = json.loads(Path(tmp / "out" / "orders" / "order-0000.json").read_text())
    assert order["suites"][0]["adapter"] == "pyperf"
    assert order["target"]["kind"] == "working_tree"


def test_pyperf_bare_paths_are_working_trees(tmp, worktrees):
    """Without a provider, a bare path is a working tree for pyperf (as it is a build for Google Benchmark)."""
    pytest.importorskip("pyperf")
    doc = session.compare_targets(
        str(worktrees["base"]), str(worktrees["head"]), profile="revisions", suite="mod.py", rounds=2,
        store=Store(tmp / "s.parquet"), adapter="pyperf", flags={"processes": 2, "values": 2, "loops": 50, "warmups": 0},
        filter=harnesses.exact_filter(["f"]), out=tmp / "out", run_key="bare")
    assert [u["workload"]["name"] for u in doc["units"]] == ["f"]
    order = json.loads(Path(tmp / "out" / "orders" / "order-0001.json").read_text())
    assert (order["suites"][0]["adapter"], order["target"]["kind"]) == ("pyperf", "working_tree")
    assert order["target"]["path"] == str(worktrees["head"].resolve())
