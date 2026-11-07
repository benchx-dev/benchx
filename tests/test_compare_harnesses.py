"""bx compare over either harness: --adapter, its precision flags, and -e.

The refusals and the orders are checked without running a harness. The last
test runs a real pyperf comparison of two revisions, end to end, and is
skipped where pyperf is not installed.
"""

import json
import os
import textwrap
from pathlib import Path

import pytest

from benchx import cli, core, harnesses, runner, session
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


PARAM_MODULE = textwrap.dedent('''
    def bench_s(n):
        sum(range(n * {work}))

    bench_s.params = [1, 2]
    bench_s.param_names = ["n"]
''')


def make_worktrees(tmp_path, module):
    """Two commits of a pure-Python suite, a clean worktree at each."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    shas = []
    for work in (200, 2000):
        (src / "mod.py").write_text(module.format(work=work))
        git(src, "add", "-A")
        git(src, "commit", "-qm", f"work {work}")
        shas.append(git(src, "rev-parse", "HEAD"))
    trees = {}
    for name, sha in zip(("base", "head"), shas):
        git(src, "worktree", "add", "-q", "--detach", str(tmp_path / name), sha)
        trees[name] = tmp_path / name
    return trees


@pytest.fixture
def worktrees(tmp_path):
    return make_worktrees(tmp_path, MODULE)


@pytest.fixture
def param_worktrees(tmp_path):
    return make_worktrees(tmp_path, PARAM_MODULE)


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


def test_each_combination_of_a_function_with_params_is_a_unit(tmp, param_worktrees):
    """Two combinations are two units, each paired with the same combination on the other side."""
    pytest.importorskip("pyperf")
    doc = session.compare_targets(
        str(param_worktrees["base"]), str(param_worktrees["head"]), profile="revisions", suite="mod.py", rounds=2,
        store=Store(tmp / "s.parquet"), adapter="pyperf", flags={"processes": 2, "values": 2, "loops": 50, "warmups": 0},
        filter=harnesses.exact_filter(["bench_s"], arguments=True), out=tmp / "out", run_key="params")
    assert sorted((u["workload"]["name"], u["workload"]["parameters"]["n"]) for u in doc["units"]) == [
        ("bench_s", "1"), ("bench_s", "2")]
    assert doc["missing"] == [] and doc["failed_invariants"] == []


def test_a_filter_that_selects_no_case_refuses_the_comparison(tmp, worktrees):
    """A comparison with nothing measured on a side is not "no difference": it is refused with the
    side's own refusal, before any case runs, and nothing reaches the store."""
    pytest.importorskip("pyperf")
    store = Store(tmp / "s.parquet")
    with pytest.raises(runner.Refused, match=r"filter '\^\(nosuch\)\$' selects no case in mod.py"):
        session.compare_targets(
            str(worktrees["base"]), str(worktrees["head"]), profile="revisions", suite="mod.py", rounds=2,
            store=store, adapter="pyperf", flags={"processes": 2, "values": 2, "loops": 50, "warmups": 0},
            filter=harnesses.exact_filter(["nosuch"]), out=tmp / "out", run_key="empty")
    assert list(store.documents("empty")) == []


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


# --- variants: one target, the two values of one parameter (UC-02) ------------------------------

VARIANTS = textwrap.dedent('''
    def bench_truncate(role):
        sum(range(10 if role == "baseline" else 2000))

    bench_truncate.params = ["baseline", "variant"]
    bench_truncate.param_names = ["role"]
''')
SIZED = textwrap.dedent('''
    def bench_truncate(role, size):
        sum(range(size if role == "baseline" else 20 * size))

    bench_truncate.params = [["baseline", "variant"], [10, 100]]
    bench_truncate.param_names = ["role", "size"]
''')
STRAY = textwrap.dedent('''
    def bench_other(size):
        sum(range(size))

    bench_other.params = [10, 100]
    bench_other.param_names = ["size"]
''')
PLAIN = "\ndef bench_plain():\n    sum(range(10))\n"
FAST = {"processes": 2, "values": 2, "loops": 50, "warmups": 0}


def variants_tree(tmp, source, files=None):
    """A clean git repository holding the suite `mod.py` (and `files`, such as an asv directory)."""
    tree = tmp / "tree"
    tree.mkdir()
    git(tree, "init", "-q")
    for name, text in {"mod.py": source, **(files or {})}.items():
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text(text)
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "suite")
    return tree


def run_variants(tmp, tree, **kwargs):
    """`session.compare_variants` on `tree`: the document, the store, and what the session said."""
    store, said = Store(tmp / "s.parquet"), []
    settings = {"suite": "mod.py", "rounds": 2, "min_rounds": 2, "adapter": "pyperf", "flags": FAST,
                "out": tmp / "out", "run_key": "v", **kwargs}
    doc = session.compare_variants(str(tree), store=store, progress=said.append, **settings)
    return doc, store, said


def measurands(store, run_key="v"):
    return [d for d in store.documents(run_key) if d["measurement"].get("kind", "measurand") == "measurand"]


def test_variants_run_one_order_per_value_and_round_with_that_values_cases_only(tmp):
    pytest.importorskip("pyperf")
    doc, store, said = run_variants(tmp, variants_tree(tmp, SIZED), parameter="role")
    assert doc["failed_invariants"] == [] and doc["missing"] == [] and doc["varying"].endswith("parameters.role")
    assert (doc["baseline"], doc["contender"]) == ("'baseline'", "'variant'")
    assert [u["workload"] for u in doc["units"]] == [
        {"name": "bench_truncate", "parameters": {"size": "10"}},
        {"name": "bench_truncate", "parameters": {"size": "100"}}]
    # Four orders, one per value and round; each holds exactly its own value's cases.
    orders = sorted((tmp / "out" / "orders").glob("order-*.json"))
    assert [o.name for o in orders] == [f"order-{slot:04d}.json" for slot in range(4)]
    for slot, path in enumerate(orders):
        value = ("baseline", "variant")[slot % 2]
        assert json.loads(path.read_text())["suites"][0]["filter"] == harnesses.exact_filter(
            [f"bench_truncate('{value}', 10)", f"bench_truncate('{value}', 100)"], arguments=True)
    # The results, by slot: baseline, variant, baseline, variant; both sizes in each.
    by_slot = {}
    for d in measurands(store):
        by_slot.setdefault(d["procedure"]["slot"], []).append(d["coordinates"]["workload"]["parameters"])
    assert sorted(by_slot) == [0, 1, 2, 3]
    assert [sorted({p["role"] for p in by_slot[slot]}) for slot in range(4)] == [
        ["'baseline'"], ["'variant'"], ["'baseline'"], ["'variant'"]]
    assert all(sorted(p["size"] for p in by_slot[slot]) == ["10", "100"] for slot in range(4))
    assert [m.split(":")[0] for m in said if m.startswith("round ")] == [
        "round 1/2 role='baseline'", "round 1/2 role='variant'", "round 2/2 role='baseline'",
        "round 2/2 role='variant'"]


def test_variants_the_baseline_is_the_first_declared_value_and_can_be_swapped(tmp):
    pytest.importorskip("pyperf")
    tree = variants_tree(tmp, VARIANTS)
    doc, _, said = run_variants(tmp, tree)
    assert doc["failed_invariants"] == [] and (doc["baseline"], doc["contender"]) == ("'baseline'", "'variant'")
    assert "varying: role (the only parameter)" in said
    assert doc["units"][0]["workload"] == {"name": "bench_truncate"} and doc["units"][0]["effect"] > 0
    swapped, _, _ = run_variants(tmp, tree, baseline="'variant'", out=tmp / "out2", run_key="v2")
    assert swapped["failed_invariants"] == [] and (swapped["baseline"], swapped["contender"]) == ("'variant'", "'baseline'")
    assert swapped["units"][0]["effect"] < 0
    # Slot 0 of the swapped run is the variant.
    assert next(d for d in measurands(Store(tmp / "s.parquet"), "v2") if d["procedure"]["slot"] == 0)[
        "coordinates"]["workload"]["parameters"] == {"role": "'variant'"}


def test_variants_an_environment_variable_is_a_second_parameter(tmp):
    pytest.importorskip("pyperf")
    tree, store = variants_tree(tmp, VARIANTS), Store(tmp / "s.parquet")
    with pytest.raises(session.SessionError, match=r"several workload parameters: OMP_NUM_THREADS \(-e\), role "
                                                   r"\(bench_truncate\): give --vary NAME"):
        run_variants(tmp, tree, environment_variables={"OMP_NUM_THREADS": "1"})
    assert not (tmp / "out").exists() and list(store.documents("v")) == []
    doc, _, _ = run_variants(tmp, tree, environment_variables={"OMP_NUM_THREADS": "1"}, parameter="role")
    assert doc["failed_invariants"] == []
    assert doc["units"][0]["workload"]["parameters"] == {"OMP_NUM_THREADS": "1"}


def test_variants_a_stray_parametrized_benchmark_needs_vary_or_a_filter(tmp):
    pytest.importorskip("pyperf")
    tree = variants_tree(tmp, VARIANTS + STRAY)
    with pytest.raises(session.SessionError) as error:
        run_variants(tmp, tree)
    assert "role (bench_truncate)" in str(error.value) and "size (bench_other)" in str(error.value)
    assert "--filter" in str(error.value) and not (tmp / "out").exists()
    doc, _, said = run_variants(tmp, tree, filter="bench_truncate")  # --filter narrows what is inferred from
    assert doc["failed_invariants"] == [] and "varying: role (the only parameter)" in said


def test_variants_a_benchmark_without_the_parameter_runs_on_neither_side_and_is_reported(tmp):
    pytest.importorskip("pyperf")
    doc, store, said = run_variants(tmp, variants_tree(tmp, VARIANTS + PLAIN))  # no --vary: bench_plain adds no name
    assert doc["failed_invariants"] == [] and [u["workload"]["name"] for u in doc["units"]] == ["bench_truncate"]
    assert "not run on either side (no parameter role): bench_plain" in said
    assert {d["coordinates"]["workload"]["name"] for d in measurands(store)} == {"bench_truncate"}


@pytest.mark.parametrize("source, kwargs, message", [
    (PLAIN, {}, "nothing to vary: no workload parameter in the selected benchmarks"),
    (VARIANTS, {"parameter": "size"}, "no selected benchmark declares a parameter 'size' \\(declared: role\\)"),
    (VARIANTS.replace('["baseline", "variant"]', '["a", "b", "c"]'), {},
     "must take the same two values of role, not \\[bench_truncate: 'a', 'b', 'c'\\]"),
    (VARIANTS + VARIANTS.replace("bench_truncate", "bench_other").replace('["baseline", "variant"]', '["old", "new"]'),
     {}, "must take the same two values of role, not \\[bench_other: 'old', 'new'; bench_truncate: 'baseline', "
         "'variant'\\]"),
    (VARIANTS, {"filter": "'baseline'"}, "must take the same two values of role, not \\[bench_truncate: 'baseline'\\]"),
    (VARIANTS, {"baseline": "variant"}, "baseline 'variant' is not a value of role; use one of: 'baseline', 'variant'"),
    (VARIANTS, {"baseline": "'varient'"}, "baseline \"'varient'\" is not a value of role; use one of: 'baseline', 'variant'"),
], ids=["no-parameter", "unknown-parameter", "three-values", "benchmarks-disagree", "filter-leaves-one-value",
        "unquoted-baseline", "mistyped-baseline"])
def test_variants_refusals_come_before_anything_runs(tmp, source, kwargs, message):
    pytest.importorskip("pyperf")
    tree, store = variants_tree(tmp, source), Store(tmp / "s.parquet")
    with pytest.raises(session.SessionError, match=message):
        run_variants(tmp, tree, **kwargs)
    assert not (tmp / "out").exists() and list(store.documents("v")) == []


def test_variants_google_benchmark_has_no_parameters_to_vary(tmp):
    with pytest.raises(session.SessionError, match="variants needs a parameter the benchmark declares"):
        session.compare_variants("nowhere", suite="s", rounds=1, store=Store(tmp / "s.parquet"),
                                 adapter="google-benchmark")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_variants_a_tree_with_no_tree_id_is_refused_before_anything_runs(tmp):
    pytest.importorskip("pyperf")
    tree = variants_tree(tmp, VARIANTS)
    unreadable = tree / "unreadable.txt"
    unreadable.write_text("x\n")
    unreadable.chmod(0)
    try:
        with pytest.raises(session.SessionError, match=r"^tree .* has no working-tree id.*unknown"):
            run_variants(tmp, tree)
    finally:
        unreadable.chmod(0o644)
    assert not (tmp / "out").exists()


def test_variants_a_dirty_tree_is_said_before_measuring_and_compared_local_only(tmp):
    pytest.importorskip("pyperf")
    tree = variants_tree(tmp, VARIANTS)
    (tree / "mod.py").write_text(VARIANTS + "# edited\n")
    doc, _, said = run_variants(tmp, tree)
    assert doc["failed_invariants"] == [] and doc["local_only"] is True
    assert f"tree {tree.resolve()} is dirty: the comparison will be local-only" in said
    assert said.index(next(m for m in said if "local-only" in m)) < said.index(
        next(m for m in said if m.startswith("round 1/2")))


ASV_CONF = '{"version": 1, "project": "uc02", "benchmark_dir": "benchmarks"}\n'
ASV_FILES = {"asv.conf.json": ASV_CONF, "benchmarks/__init__.py": "", "benchmarks/trunc.py": textwrap.dedent('''
    def time_truncate(role):
        sum(range(10 if role == "baseline" else 2000))

    time_truncate.params = ["baseline", "variant"]
    time_truncate.param_names = ["role"]

    def time_other(size):
        sum(range(size))

    time_other.params = [10, 100]
    time_other.param_names = ["size"]
''')}
ASV_FAST = {"processes": 2, "values": 2, "warmups": 0, "min_time": 0.001}


def test_variants_asv_cases_are_split_by_value_under_their_module_qualified_names(tmp):
    from benchx import adapters
    tree = variants_tree(tmp, "", ASV_FILES)
    asv, env = adapters.get("asv"), dict(os.environ)
    runnable = asv.locate(tree, "benchmarks", env)
    cases = asv.list_cases(runnable, None, env)
    by_value, without = session._split_cases(asv, runnable, cases, "role")
    assert without == ["trunc.time_other"]  # it has no `role`: it runs on neither side
    assert by_value == {"'baseline'": ["trunc.time_truncate('baseline')"],
                        "'variant'": ["trunc.time_truncate('variant')"]}
    for value, names in by_value.items():  # each side's filter selects exactly that side's cases
        selected = asv.list_cases(runnable, harnesses.exact_filter(names, arguments=True), env)
        assert [asv.workload(case, runnable) for case in selected] == [("trunc.time_truncate", {"role": value})]


def test_variants_asv_end_to_end(tmp):
    tree = variants_tree(tmp, "", ASV_FILES)
    doc, store, said = run_variants(tmp, tree, suite="benchmarks", adapter="asv", flags=ASV_FAST, parameter="role")
    assert doc["failed_invariants"] == [] and [u["workload"] for u in doc["units"]] == [{"name": "trunc.time_truncate"}]
    assert {d["coordinates"]["workload"]["name"] for d in measurands(store)} == {"trunc.time_truncate"}
    assert "not run on either side (no parameter role): trunc.time_other" in said
