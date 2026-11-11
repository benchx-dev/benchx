"""Which build a benchmark imports, when the sides are installed builds (UC-03).

A `spin` build keeps its package deep inside the build directory
(`<build>/usr/lib/python3.X/site-packages/numpy`), not at the build's root. pyperf
puts the target's directory first on `sys.path`, so what `import` finds depends on
the `target.path` the provider reports. The toy here is a pure-Python package built
twice into trees laid out like that, doing 100 times the work in the second, with
the suite committed in a repository of its own. Each test runs `bx compare` in
measure mode over the two builds, with one thing varied:

- the provider reports the build root and nothing is exported: nothing imports;
- the provider reports the build root and one `PYTHONPATH` is exported: both sides
  import the build it names, and the comparison does not notice;
- the provider reports the directory that holds the package: each side imports its
  own build.

Every run uses real pyperf, and is skipped where pyperf is not installed.
"""

import textwrap

import pytest

from benchx import harnesses, session
from benchx.store import Store
from conftest import SOURCE_URI, git

WORK = {"base": 200, "head": 20000}  # what `toy.work()` does in each build, as a range length
SUITE = textwrap.dedent('''
    def bench_work():
        import toy
        toy.work()
''')


@pytest.fixture
def toy(tmp_path):
    """Two clean checkouts, a build of each laid out like spin's, and a committed
    suite outside both."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    shas = []
    for work in WORK.values():
        (src / "toy_source.txt").write_text(f"WORK = {work}\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", f"work {work}")
        shas.append(git(src, "rev-parse", "HEAD"))
    sides = {}
    for (name, work), sha in zip(WORK.items(), shas):
        checkout = tmp_path / name
        git(src, "worktree", "add", "-q", "--detach", str(checkout), sha)
        build = tmp_path / f"build-{name}"
        package = build / "usr" / "lib" / "python3" / "site-packages"
        (package / "toy").mkdir(parents=True)
        (package / "toy" / "__init__.py").write_text(f"def work():\n    sum(range({work}))\n")
        sides[name] = {"checkout": checkout, "build": build, "package": package}
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "bench_toy.py").write_text(SUITE)
    git(suite, "init", "-q")
    git(suite, "add", "-A")
    git(suite, "commit", "-qm", "suite")
    return {"sides": sides, "suite": suite / "bench_toy.py"}


def compare(tmp, toy, monkeypatch, *, reported):
    """`bx compare @base @head` over the toy builds; `reported` names the directory
    each provider answer gives as `target.path`: "build" or "package"."""
    def provider(spec, operation, provider=None):
        side = toy["sides"][spec.removeprefix("@")]
        return {"schema_version": "benchx/target-description/0.1.0",
                "target": {"kind": "build", "path": str(side[reported]),
                           "source": {"uri": SOURCE_URI, "type": "git"}, "source_dir": str(side["checkout"])},
                "how_built": f"toy build {spec}"}

    monkeypatch.setattr(session.target_mod, "resolve", provider)
    doc = session.compare_targets(
        "@base", "@head", profile="revisions", suite=str(toy["suite"]), rounds=3, store=Store(tmp / "s.parquet"),
        adapter="pyperf", flags={"processes": 2, "values": 2, "loops": 50, "warmups": 1},
        filter=harnesses.exact_filter(["bench_work"]), out=tmp / "out", run_key="toy")
    assert doc["failed_invariants"] == []
    assert [u["workload"]["name"] for u in doc["units"]] == ["bench_work"]
    return doc


def test_the_build_root_imports_nothing(tmp, toy, monkeypatch):
    """The package is below the target's root, so `import toy` fails in every case: no
    result, and no verdict."""
    pytest.importorskip("pyperf")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    doc = compare(tmp, toy, monkeypatch, reported="build")
    unit = doc["units"][0]
    assert unit["rounds"] == 0
    assert unit["verdict"] == "indeterminate"
    assert doc["exclusions"] and {e["reason"] for e in doc["exclusions"]} == {"error: harness.error"}


def test_one_pythonpath_measures_one_build_on_both_sides(tmp, toy, monkeypatch):
    """What the comparison does today, kept as a record rather than as a wish: an
    exported `PYTHONPATH` names one build, both sides import it, and the 100-fold
    difference between the builds goes unnoticed. The verdict is not asserted: with
    the same code on both sides it follows the noise of three paired rounds (one run
    in eight said "improved", by under 4%), but the effect stays near zero."""
    pytest.importorskip("pyperf")
    monkeypatch.setenv("PYTHONPATH", str(toy["sides"]["base"]["package"]))
    doc = compare(tmp, toy, monkeypatch, reported="build")
    unit = doc["units"][0]
    assert unit["rounds"] == 3
    assert abs(unit["effect"]) < 1  # a real 100-fold difference is an effect of about 100


def test_the_package_directory_as_target_imports_each_build(tmp, toy, monkeypatch):
    """With the directory that holds the package as the target path, pyperf's own
    `sys.path` entry finds each side's build, in one shared environment."""
    pytest.importorskip("pyperf")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    doc = compare(tmp, toy, monkeypatch, reported="package")
    unit = doc["units"][0]
    assert unit["rounds"] == 3
    assert unit["verdict"] == "regressed"
    assert unit["effect"] > 10
