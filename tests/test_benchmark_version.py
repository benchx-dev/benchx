"""The version of a pyperf benchmark function, and the refusal of a suite directory outside the
checkout (PYPERF_VERSION_PLAN.md).

The version is what asv calls it: a hash of the function's source, which the result records in
`protocol.pyperf.benchmark_version` so that two sides that ran different code do not agree.
"""

import hashlib
import importlib.util
import itertools
import json
import textwrap

import pytest

from benchx.adapters import benchx_params as params
from benchx.store import Store

_counter = itertools.count()


def load(tmp_path, text, name="f"):
    """The function `name` of a module made of `text`, in a file of its own (inspect reads source files)."""
    path = tmp_path / f"m{next(_counter)}.py"
    path.write_text(textwrap.dedent(text))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def version(tmp_path, text, name="f"):
    return params.source_version(load(tmp_path, text, name))


SOURCE = "def f(n):\n    sum(range(n))"


def test_the_version_is_a_hash_of_the_dedented_source(tmp_path):
    """As asv_runner makes it for a plain function: lines stripped of trailing whitespace, dedented."""
    assert version(tmp_path, SOURCE + "\n") == hashlib.sha256(SOURCE.encode()).hexdigest()


def test_trailing_whitespace_and_blank_lines_after_the_function_are_not_a_change(tmp_path):
    assert version(tmp_path, "def f(n):   \n    sum(range(n))  \n\n\n") == version(tmp_path, SOURCE + "\n")


@pytest.mark.parametrize(
    "edited",
    [
        "def f(n):\n    sum(range(n + 1))",  # the code
        "def f(n):\n    # a comment\n    sum(range(n))",  # a comment: asv's primary hash counts it
        'def f(n):\n    """a docstring"""\n    sum(range(n))',  # a docstring
        "def g(n):\n    sum(range(n))",  # the name
    ],
)
def test_an_edit_of_the_function_is_a_new_version(tmp_path, edited):
    assert version(tmp_path, edited, name=edited.split("(")[0].split()[-1]) != version(tmp_path, SOURCE + "\n")


def test_what_is_not_the_function_is_not_in_its_version(tmp_path):
    f = load(tmp_path, SOURCE + "\n")
    before = params.source_version(f)
    f.params, f.param_names = [[1, 2, 3]], ["n"]  # the values of params are the workload's, not the code's
    assert params.source_version(f) == before
    helper = "def helper():\n    return {value}\n\ndef f():\n    return helper()\n"
    assert version(tmp_path, helper.format(value=1)) == version(tmp_path, helper.format(value=2))  # a limit, as in asv


def test_an_explicit_version_wins(tmp_path):
    f = load(tmp_path, SOURCE + "\n")
    f.version = 3
    assert params.source_version(f) == "3"
    g = load(tmp_path, "def f(n):\n    sum(range(n + 1))\n")
    g.version = "3"
    assert params.source_version(g) == params.source_version(f)  # two different bodies, one declared version


def test_a_function_without_source_has_no_version():
    namespace = {}
    exec("def f():\n    pass\n", namespace)  # no file: inspect cannot read it
    assert params.source_version(namespace["f"]) is None
    assert params.source_version(len) is None  # nor can a built-in
    namespace["f"].version = "v1"
    assert params.source_version(namespace["f"]) == "v1"  # but a declared version needs no source


# --- through the adapter ---------------------------------------------------------------------

MODULE = textwrap.dedent("""
    def bench_a():
        sum(range(100))

    def bench_b(n):
        sum(range(n))

    bench_b.params = [1, 2]
    bench_b.param_names = ["n"]
""")


@pytest.fixture
def tree(tmp_path):
    path = tmp_path / "tree"
    path.mkdir()
    (path / "mod.py").write_text(MODULE)
    return path


def test_the_listing_carries_the_version_of_each_function(tree):
    pytest.importorskip("pyperf")
    import os

    from benchx.adapters import pyperf as adapter

    env = dict(os.environ)
    runnable = adapter.locate(tree, "mod.py", env)
    adapter.list_cases(runnable, None, env)
    versions = {name: info["version"] for name, info in runnable["benchmarks"].items()}
    assert versions == {
        "bench_a": params.source_version(load(tree.parent, MODULE, "bench_a")),
        "bench_b": params.source_version(load(tree.parent, MODULE, "bench_b")),
    }
    assert versions["bench_a"] != versions["bench_b"]


def test_each_result_records_the_version_of_its_function(tmp_path, tree):
    pytest.importorskip("pyperf")
    from test_pyperf import pyperf_order, run_order

    docs = run_order(tmp_path, pyperf_order(tree, "recorded", case_filter="bench_"), "recorded")
    recorded = {}
    for d in docs:
        recorded.setdefault(d["coordinates"]["workload"]["name"], set()).add(
            d["coordinates"]["protocol"]["pyperf"]["benchmark_version"]
        )
    expected = {name: {params.source_version(load(tmp_path, MODULE, name))} for name in ("bench_a", "bench_b")}
    assert recorded == expected  # one version for every combination of bench_b, and not the other's


def test_the_protocol_has_the_version_only_when_there_is_one():
    pytest.importorskip("pyperf")
    from benchx.adapters import pyperf as adapter

    run = {"native": None, "benchmark_version": None}
    assert adapter.case_protocol({"timer": "perf_counter"}, run, "bench_a") == {"timer": "perf_counter"}
    run["benchmark_version"] = "abc"
    assert adapter.case_protocol({"timer": "perf_counter"}, run, "bench_a") == {
        "timer": "perf_counter",
        "pyperf": {"benchmark_version": "abc"},
    }


def test_an_asv_result_has_its_own_version_and_no_pyperf_one(tmp_path):
    pytest.importorskip("pyperf")
    from benchx import cli

    tree = tmp_path / "asv-tree"
    (tree / "benchmarks").mkdir(parents=True)
    (tree / "benchmarks" / "bm.py").write_text("class T:\n    def time_x(self):\n        sum(range(100))\n")
    out = tmp_path / "out"
    code = cli.main(
        [
            "bench",
            str(tree),
            "--adapter",
            "asv",
            "--suite",
            "benchmarks",
            "--run-key",
            "asv",
            "--no-ingest",
            "--processes",
            "2",
            "--values",
            "2",
            "--out",
            str(out),
        ]
    )
    assert code == cli.EXIT_OK
    docs = [json.loads(p.read_text()) for p in out.glob("**/*wall-time*.json")]
    assert docs and all(
        "pyperf" not in d["coordinates"]["protocol"] and d["coordinates"]["protocol"]["asv"]["benchmark_version"]
        for d in docs
    )


# --- two revisions, the function in the tree --------------------------------------------------

FLAGS = {"processes": 2, "values": 2, "loops": 50, "warmups": 0}


def compare(tmp_path, module, run_key, names=("f",)):
    pytest.importorskip("pyperf")
    pytest.importorskip("pyarrow")
    from benchx import harnesses, session
    from test_compare_harnesses import make_worktrees

    trees = make_worktrees(tmp_path, module)
    return session.compare_targets(
        str(trees["base"]),
        str(trees["head"]),
        profile="revisions",
        suite="mod.py",
        rounds=2,
        store=Store(tmp_path / "s.parquet"),
        adapter="pyperf",
        flags=FLAGS,
        filter=harnesses.exact_filter(list(names)),
        out=tmp_path / "out",
        run_key=run_key,
    )


def test_two_revisions_whose_function_differs_are_not_comparable(tmp_path):
    doc = compare(tmp_path, "def f():\n    sum(range({work}))\n", "edited")
    assert doc["failed_invariants"] == []
    assert [(u["workload"]["name"], u["reason"]) for u in doc["units"]] == [("f", "benchmark-changed")]


def test_editing_one_function_leaves_the_others_compared(tmp_path):
    """As in asv: only the edited function is not comparable, the comparison goes on for the rest."""
    module = "def f():\n    sum(range({work}))\n\n\ndef g():\n    sum(range(500))\n"
    doc = compare(tmp_path, module, "two", names=("f", "g"))
    assert doc["failed_invariants"] == []
    units = {u["workload"]["name"]: u for u in doc["units"]}
    assert units["f"]["reason"] == "benchmark-changed" and units["f"]["details"]["rounds"] == 0
    assert (
        units["g"]["details"]["rounds"] == 2 and units["g"].get("reason") != "benchmark-changed"
    )  # paired, not set aside


def test_two_revisions_that_differ_outside_the_function_agree(tmp_path):
    """The code under test is elsewhere (here, a constant of the module): the function is the same."""
    doc = compare(tmp_path, "WORK = {work}\n\ndef f():\n    sum(range(WORK))\n", "outside")
    assert doc["failed_invariants"] == [] and [u["workload"]["name"] for u in doc["units"]] == ["f"]


def test_a_declared_version_keeps_two_revisions_comparable(tmp_path):
    """A function that is itself the subject, pinned: its body changes, its version does not."""
    doc = compare(tmp_path, "def f():\n    sum(range({work}))\n\nf.version = '1'\n", "pinned")
    assert doc["failed_invariants"] == [] and [u["workload"]["name"] for u in doc["units"]] == ["f"]


# --- a suite directory outside the checkout ---------------------------------------------------


def test_an_asv_suite_outside_the_checkout_is_refused_with_a_message(tmp_path, capsys):
    pytest.importorskip("pyperf")
    from benchx import cli

    tree, suite = tmp_path / "tree", tmp_path / "suite"
    tree.mkdir()
    suite.mkdir()
    (tree / "x.txt").write_text("x\n")
    (suite / "bm.py").write_text("class T:\n    def time_x(self):\n        sum(range(100))\n")
    import subprocess

    for directory in (tree, suite):
        for args in (
            ["init", "-q"],
            ["add", "-A"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"],
        ):
            subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True)
    code = cli.main(
        [
            "bench",
            str(tree),
            "--adapter",
            "asv",
            "--suite",
            str(suite),
            "--run-key",
            "out",
            "--no-ingest",
            "--processes",
            "2",
            "--values",
            "2",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "bx bench: error:" in err and "outside the subject's checkout" in err and "put it inside" in err
    assert "Traceback" not in err
    assert not list((tmp_path / "out").glob("**/*.json"))  # nothing ran, nothing was written


def test_what_is_found_outside_the_checkout_is_a_file_or_it_is_refused(tmp_path):
    from benchx import harnesses
    from benchx.errors import SessionError
    from benchx.session import _found_benchmark

    source, build, elsewhere = tmp_path / "source", tmp_path / "build", tmp_path / "elsewhere"
    for directory in (source / "benchmarks", build, elsewhere):
        directory.mkdir(parents=True)
    (elsewhere / "m.py").write_text("def bench_a():\n    pass\n")
    asv, pyperf = harnesses.get("asv"), harnesses.get("pyperf")
    # a suite inside the source checkout, with the target the build elsewhere (NumPy through the provider)
    assert _found_benchmark(asv, build, str(source / "benchmarks"), str(source)) is None
    # a pyperf file outside the checkout is the benchmark's own source, identified by the file
    assert _found_benchmark(pyperf, source, str(elsewhere / "m.py"), str(source))["tree"]
    # a directory outside has no such identity
    with pytest.raises(SessionError, match="outside the subject's checkout"):
        _found_benchmark(asv, source, str(elsewhere), str(source))
