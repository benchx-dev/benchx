"""`bx bench` (PYPERF_PLAN.md step 5)."""

import json
import textwrap

import pytest

pytest.importorskip("pyperf")

from benchx import bench, cli, core
from benchx.store import Store

MODULE = textwrap.dedent('''
    def bench_a():
        sum(range(100))

    def bench_b():
        sum(range(200))

    def helper():
        pass
''')
FAST = ["--processes", "2", "--values", "2", "--loops", "20"]


def levels(precision):
    return {lv["unit"]: lv["n"] for lv in precision["repetitions"]["levels"]}


def write_config(directory, precision):
    path = directory / ".benchx" / "config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"adapters": {"pyperf": {"precision": precision}}}))
    return path


@pytest.fixture
def tree(tmp, monkeypatch):
    path = tmp / "tree"
    path.mkdir()
    (path / "mod.py").write_text(MODULE)
    monkeypatch.chdir(path)
    return path


def test_order_holds_the_resolved_precision(tree):
    write_config(tree, {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 7}]}})
    order, _ = bench.build_order(tree / "mod.py", ["bench_a"], precision_flags=bench.flags_to_precision(values=5),
                                 run_key="r", cwd=tree)
    assert core.check(order, "work-order") == []
    assert levels(order["precision"]) == {"process": 7, "value": 5}
    assert order["suites"][0] == {"adapter": "pyperf", "suite": "mod.py", "filter": "^(bench_a)$"}
    assert order["target"]["kind"] == "working_tree"


def test_both_calibration_flags_conflict():
    with pytest.raises(bench.SessionError):
        bench.flags_to_precision(min_time=0.1, loops=5)


def test_bench_command_runs_and_ingests(tree, capsys):
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "cli", "--project", "p", *FAST]) == cli.EXIT_OK
    out = capsys.readouterr()
    assert "bench_a" in out.out and "success" in out.out and "bench_b" not in out.out
    assert "2 processes x 2 values" in out.err
    results = [d for d in Store().documents("cli")]
    assert [d["coordinates"]["workload"]["name"] for d in results] == ["bench_a"]
    order = json.loads(next((tree / "results" / "cli").glob("workorder-*.json")).read_text())
    assert levels(order["precision"]) == {"process": 2, "value": 2}


def test_unversioned_benchmark_is_identified_by_its_tree(tree):
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "nogit", "--no-ingest", *FAST]) == cli.EXIT_OK
    documents = [json.loads(p.read_text()) for p in (tree / "results" / "nogit").glob("*.json")
                 if not p.name.startswith("workorder-")]
    assert documents
    for document in documents:
        assert core.check(document, "measurement-result") == []
        provenance = document["provenance"]
        assert "revision" not in document["benchmark"]
        assert provenance["benchmark_dirty"] == "dirty"
        assert provenance["benchmark_tree"] == provenance["subject_tree"]


def test_bench_without_names_runs_the_default_cases(tree, capsys):
    assert cli.main(["bench", "mod.py", "--run-key", "all", "--no-ingest", *FAST]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "bench_a" in out and "bench_b" in out and "helper" not in out


def test_bench_reports_a_bad_config_before_running(tree, capsys):
    write_config(tree, {"calibration": {"mode": "fixed"}})
    assert cli.main(["bench", "mod.py", "--no-ingest", *FAST]) == cli.EXIT_REFUSED
    assert "n_iterations" in capsys.readouterr().err
    assert not (tree / "results").exists()


def test_bench_rejects_a_missing_module_and_bad_env(tree, capsys):
    assert cli.main(["bench", "nope.py"]) == cli.EXIT_REFUSED
    assert cli.main(["bench", "mod.py", "-e", "NOEQUALS"]) == cli.EXIT_REFUSED
    assert not (tree / "results").exists()
