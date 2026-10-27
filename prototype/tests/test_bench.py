"""`bx bench` (PYPERF_PLAN.md step 5)."""

import json
import textwrap

import pytest

from benchx.identity import normalize_remote

pytest.importorskip("pyperf")

from benchx import cli, config, core, harnesses, session
from benchx.errors import SessionError
from benchx.store import Store
from conftest import SOURCE_URI

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
    path = directory / "benchx.conf.json"
    path.write_text(json.dumps({"schema_version": "benchx/config/0.1.0",
                                "adapter": {"name": "pyperf", "precision": precision}}))
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
    cfg = config.load(tree)
    harness, precision = session._harness("pyperf", {"values": 5}, cfg.adapter["precision"])
    side = session._side(str(tree), "mod.py", "describe", harness)
    order = session._order(side, harness=harness, suite="mod.py", filter=harnesses.exact_filter(["bench_a"]),
                           quantities=["wall-time"], precision=precision, environment_variables=None, project=None,
                           run_key="r", source_uri=SOURCE_URI)
    assert cfg.path == tree / "benchx.conf.json"
    assert core.check(order, "work-order") == []
    assert levels(order["precision"]) == {"process": 7, "value": 5}
    assert order["suites"][0] == {"adapter": "pyperf", "suite": "mod.py", "filter": "^(bench_a)$"}
    assert order["target"]["kind"] == "working_tree"
    assert "round" not in order and "slot" not in order


def test_both_calibration_flags_conflict():
    with pytest.raises(SessionError):
        harnesses.PYPERF.flags_to_precision(min_time=0.1, loops=5)


def test_bench_command_runs_and_ingests(tree, capsys):
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "cli", "--project", "p", *FAST]) == cli.EXIT_OK
    out = capsys.readouterr()
    assert "bench_a" in out.out and "success" in out.out and "bench_b" not in out.out
    assert "2 processes x 2 values" in out.err
    results = [d for d in Store(tree / "store.parquet").documents("cli") if d["measurement"]["quantity"]["name"] == "wall-time"]
    assert [d["coordinates"]["workload"]["name"] for d in results] == ["bench_a"]
    order = json.loads(next((tree / "results" / "cli").glob("workorder-*.json")).read_text())
    assert levels(order["precision"]) == {"process": 2, "value": 2}


def test_shorthand_order_is_stable(tree):
    """The order `bx bench MODULE.py FUNCTION` writes; unchanged by how bench is wired inside."""
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "snap", "--no-ingest", "-e", "A=1", *FAST]) == cli.EXIT_OK
    order = json.loads((tree / "results" / "snap" / "orders" / "order.json").read_text())
    here = str(tree.resolve())
    assert order.pop("work_order_id").startswith("urn:uuid:")
    assert order == {
        "schema_version": "benchx/work-order/0.1.0", "state": "resolved",
        "suites": [{"adapter": "pyperf", "suite": "mod.py", "filter": "^(bench_a)$"}],
        "target": {"kind": "working_tree", "path": here, "source_dir": here,
                   "source": {"uri": f"file://{here}", "type": "git"}},
        "quantities": ["wall-time"],
        "precision": {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 2}, {"unit": "value", "n": 2}]},
                      "calibration": {"mode": "fixed", "n_iterations": 20},
                      "warmup": {"mode": "count", "n_warmup": 1}},
        "timeouts": {"case_s": 60, "order_s": 600}, "run_key": "snap",
        "requester": {"kind": "workbench", "name": "bx"},
        "plan": [{"id": "p0", "case": "*", "quantity": "wall-time"}],
        "environment_variables": {"A": "1"},
    }


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


@pytest.mark.parametrize("remote, uri", [
    ("git@github.com:ev-br/benchx.git", "ssh://git@github.com/~/ev-br/benchx.git"),
    ("git@host:/srv/git/benchx.git", "ssh://git@host/srv/git/benchx.git"),
    ("host:repos/x.git", "ssh://host/~/repos/x.git"),
    ("git@[::1]:repo.git", "ssh://git@[::1]/~/repo.git"),
    ("git@[::1]:/srv/repo.git", "ssh://git@[::1]/srv/repo.git"),
    ("https://github.com/ev-br/benchx.git", "https://github.com/ev-br/benchx.git"),
    ("ssh://git@github.com/ev-br/benchx.git", "ssh://git@github.com/ev-br/benchx.git"),
    ("/srv/git/benchx.git", "file:///srv/git/benchx.git"),
    ("C:\\src\\benchx", "file:///C:/src/benchx"),
    ("file:///srv/git/benchx.git", "file:///srv/git/benchx.git"),
])
def test_remotes_become_uris(remote, uri):
    assert normalize_remote(remote) == uri
    assert normalize_remote(uri) == uri  # idempotent


def test_relative_local_remotes_resolve_against_base(tmp_path):
    assert normalize_remote("../upstream", base=str(tmp_path / "work")) == (tmp_path / "upstream").as_uri()


def test_source_uri_flag_overrides_the_remote(tree):
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "uri", "--no-ingest", "--source-uri",
                     "https://example.org/r.git", *FAST]) == cli.EXIT_OK
    order = json.loads((tree / "results" / "uri" / "orders" / "order.json").read_text())
    assert order["target"]["source"]["uri"] == "https://example.org/r.git"


def test_bench_places_calls_in_a_session(tree):
    """--label, --round and --slot make separate `bx bench` calls the sides of one session."""
    sides = [("a", 0), ("b", 0), ("a", 1), ("b", 1)]
    for slot, (side, round_) in enumerate(sides):
        assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "S", "--label", f"env={side}",
                         "--round", str(round_), "--slot", str(slot), "--no-ingest", *FAST]) == cli.EXIT_OK
    out = tree / "results" / "S"
    assert sorted(p.name for p in (out / "orders").glob("*.json")) == [f"order-{n:04d}.json" for n in range(4)]
    found = []
    for path in out.glob("*.json"):
        document = json.loads(path.read_text())
        if path.name.startswith("workorder-") or document["measurement"].get("kind") == "condition":
            continue
        assert core.check(document, "measurement-result") == []
        found.append((document["procedure"]["slot"], document["procedure"]["round"],
                      document["provenance"]["labels"]))
    assert sorted(found, key=lambda f: f[0]) == [(0, 0, {"env": "a"}), (1, 0, {"env": "b"}),
                                                  (2, 1, {"env": "a"}), (3, 1, {"env": "b"})]


@pytest.mark.parametrize("extra, message", [
    (["--label", "env=a", "--round", "0", "--slot", "0"], "explicit --run-key"),
    (["--slot", "0"], "explicit --run-key"),
    (["--run-key", "S", "--label", "env=a"], "need --slot"),
    (["--run-key", "S", "--round", "0"], "need --slot"),
    (["--run-key", "S", "--slot", "0", "--label", "env"], "NAME=VALUE"),
    (["--run-key", "S", "--slot", "0", "--label", "env=a", "--label", "env=b"], "given twice"),
    (["--run-key", "S", "--slot", "-1"], "from 0"),
])
def test_session_flags_are_refused_before_anything_runs(extra, message, tree, capsys):
    assert cli.main(["bench", "mod.py", "bench_a", "--no-ingest", *extra, *FAST]) == cli.EXIT_REFUSED
    assert message in capsys.readouterr().err
    assert not (tree / "results").exists()


def test_bench_without_session_flags_is_unchanged(tree):
    assert cli.main(["bench", "mod.py", "bench_a", "--run-key", "plain", "--no-ingest", *FAST]) == cli.EXIT_OK
    assert (tree / "results" / "plain" / "orders" / "order.json").exists()
    order = json.loads((tree / "results" / "plain" / "orders" / "order.json").read_text())
    assert not {"round", "slot", "labels"} & set(order)
