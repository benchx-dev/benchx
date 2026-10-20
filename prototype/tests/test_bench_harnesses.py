"""bx bench over either harness: the same target, options and order builder as bx compare.

Google Benchmark runs against the fake binary, so these run anywhere; the
pyperf cases are skipped where pyperf is not installed.
"""

import json
import stat
import sys
import textwrap

import pytest

from benchx import cli, core
from benchx.store import Store
from conftest import QUIET, NOISY, cases, git, make_build
from test_target import PROVIDER, describe

GBENCH = ["--adapter", "google-benchmark", "--suite", "demo-bench"]


@pytest.fixture
def build(repo, tmp, monkeypatch):
    monkeypatch.chdir(tmp)
    return make_build(tmp / "build", repo["wt-head"], cases())


def order_of(tmp, run_key):
    return json.loads((tmp / "results" / run_key / "orders" / "order.json").read_text())


def test_one_google_benchmark_run_prints_a_table_and_delivers(build, tmp, capsys):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "g1"]) == cli.EXIT_OK
    out = capsys.readouterr()
    assert QUIET in out.out and NOISY in out.out and "success" in out.out
    assert "5 repetitions" in out.err  # the default, stated
    order = order_of(tmp, "g1")
    assert core.check(order, "work-order") == []
    assert (order["suites"][0], order["target"]["kind"]) == (
        {"adapter": "google-benchmark", "suite": "demo-bench"}, "build")
    assert "round" not in order and "slot" not in order
    assert {d["coordinates"]["workload"]["name"] for d in Store().documents("g1")} == {QUIET, NOISY}


def test_head_table_prints_the_bench_columns_of_the_stored_results(build, capsys):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "h1"]) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["head", "--table"]) == cli.EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split()[:3] == ["revision", "run", "project"]
    assert all("h1" in line and "success" in line for line in lines[3:]) and len(lines) == 5
    assert QUIET in lines[3] + lines[4] and NOISY in lines[3] + lines[4]
    with pytest.raises(SystemExit):  # one shape at a time
        cli.main(["head", "--table", "--json"])


def test_head_filters_by_project_and_workload_before_taking_n(build, capsys):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "f1", "--project", "alpha"]) == cli.EXIT_OK
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "f2", "--project", "beta"]) == cli.EXIT_OK
    capsys.readouterr()

    def listed(*args):
        assert cli.main(["head", "--table", *args]) == cli.EXIT_OK
        return capsys.readouterr().out.splitlines()[3:]

    assert len(listed("-n", "100")) == 4
    # the two newest rows are beta's; alpha's are older, and -n 2 still finds them
    assert [line.split()[1:3] for line in listed("-n", "2", "--project", "alpha")] == [["f1", "alpha"]] * 2
    only = listed("-n", "100", "--workload", "Noisy")
    assert len(only) == 2 and all(NOISY in line for line in only)
    assert [line.split()[1:3] for line in listed("--project", "beta", "--workload", "Quiet")] == [["f2", "beta"]]
    assert listed("--project", "nonesuch") == []


def test_the_source_comes_from_cmake_cache(build, repo, tmp):
    assert cli.main(["bench", str(build), *GBENCH, "--run-key", "g2", "--no-ingest"]) == cli.EXIT_OK
    assert order_of(tmp, "g2")["target"]["source_dir"] == str(repo["wt-head"].resolve())
    assert not Store().path.exists()  # --no-ingest


def test_cases_are_named_or_filtered(build, tmp, capsys):
    assert cli.main(["bench", str(build), QUIET, *GBENCH, "--run-key", "c1", "--no-ingest"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert QUIET in out and NOISY not in out
    assert order_of(tmp, "c1")["suites"][0]["filter"] == "^(BM_Quiet/1024)$"
    assert cli.main(["bench", str(build), *GBENCH, "--filter", "Noisy", "--run-key", "c2", "--no-ingest"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert NOISY in out and QUIET not in out


def test_precision_and_quantity_are_the_harnesss(build, tmp):
    assert cli.main(["bench", str(build), *GBENCH, "--repetitions", "3", "--quantity", "cpu-time",
                     "--run-key", "p1", "--no-ingest"]) == cli.EXIT_OK
    order = order_of(tmp, "p1")
    assert order["precision"]["repetitions"] == 3 and order["quantities"] == ["cpu-time"]


@pytest.mark.parametrize("argv, message", [
    (["BUILD", *GBENCH, "--processes", "2"], "google-benchmark takes no --processes"),
    (["BUILD", *GBENCH, "--loops", "5"], "google-benchmark takes no --loops"),
    (["BUILD", "--suite", "mod.py", "--repetitions", "3"], "pyperf takes no --repetitions"),  # the default
    (["BUILD", *GBENCH, "--filter", "Quiet", QUIET], "case names or --filter"),
    (["BUILD", *GBENCH, "--tree", "t"], "--tree belongs to"),
    (["BUILD", "--adapter", "google-benchmark"], "give --suite, or a Python file"),
    (["m.py", "--adapter", "google-benchmark"], "benchmarked with pyperf"),
])
def test_what_does_not_fit_is_refused_before_anything_runs(argv, message, build, tmp, capsys):
    assert cli.main(["bench", *(str(build) if a == "BUILD" else a for a in argv)]) == cli.EXIT_REFUSED
    assert message in capsys.readouterr().err
    assert not (tmp / "results").exists()


def test_a_suite_the_adapter_cannot_find_is_refused(build, tmp, capsys):
    assert cli.main(["bench", str(build), "--adapter", "google-benchmark", "--suite", "gone"]) == cli.EXIT_REFUSED
    assert "suite binary not found" in capsys.readouterr().err
    assert not (tmp / "results").exists()


def test_a_provider_target_is_resolved_like_compare_does(repo, build, tmp, monkeypatch, capsys):
    descriptions = tmp / "descriptions.json"
    descriptions.write_text(json.dumps({"plain": describe(build, repo["wt-head"], how_built="cmake plain")}))
    script = tmp / "proj" / "provider.py"
    script.parent.mkdir()
    script.write_text(PROVIDER.format(python=sys.executable, descriptions=str(descriptions)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (tmp / "proj" / ".benchx").mkdir()
    (tmp / "proj" / ".benchx" / "provider.json").write_text(json.dumps({"command": [str(script)]}))
    monkeypatch.chdir(tmp / "proj")
    assert cli.main(["bench", "@plain", *GBENCH, "--run-key", "pr1"]) == cli.EXIT_OK
    assert QUIET in capsys.readouterr().out
    [doc, *_] = Store().documents("pr1")
    assert doc["provenance"]["info"]["target_provider"]["version"] == "prepare"
    assert cli.main(["bench", "@plain", *GBENCH, "--run-key", "pr2", "--no-build", "--no-ingest"]) == cli.EXIT_OK
    result = next(p for p in (tmp / "proj" / "results" / "pr2").glob("*.json") if not p.name.startswith("workorder-"))
    assert json.loads(result.read_text())["provenance"]["info"]["target_provider"]["version"] == "describe"


MODULE = textwrap.dedent('''
    def bench_a():
        sum(range(100))
''')


def test_pyperf_takes_a_working_tree_path_like_the_shorthand(tmp, monkeypatch, capsys):
    pytest.importorskip("pyperf")
    tree = tmp / "wt"
    (tree / "benchmarks").mkdir(parents=True)
    (tree / "benchmarks" / "mod.py").write_text(MODULE)
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "m")
    monkeypatch.chdir(tmp)
    fast = ["--processes", "2", "--values", "2", "--loops", "20", "--no-ingest"]
    assert cli.main(["bench", str(tree), "--suite", "benchmarks/mod.py", "bench_a", "--run-key", "w1", *fast]) == cli.EXIT_OK
    assert "bench_a" in capsys.readouterr().out
    long_form = order_of(tmp, "w1")
    assert cli.main(["bench", str(tree / "benchmarks" / "mod.py"), "bench_a", "--run-key", "w2", *fast]) == cli.EXIT_OK
    short_form = order_of(tmp, "w2")
    for order in (long_form, short_form):  # the shorthand is the long form, spelled shorter
        order.pop("work_order_id"), order.pop("run_key")
    assert long_form == short_form
    assert long_form["target"]["kind"] == "working_tree"
