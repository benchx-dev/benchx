"""End to end: two NumPy revisions built by `spin`, compared in one `bx compare` (UC-03).

Opt-in. It builds NumPy with `spin` (a few minutes per revision the first time) and
needs network for the submodules, so it runs only when these are set:

    BENCHX_E2E_NUMPY       a git checkout of numpy; the provider adds worktrees to it
                           and never builds in it
    BENCHX_E2E_EXAMPLE     the `numpy-revisions` example directory (benchx_user)

and optionally:

    BENCHX_E2E_WORKTREES   where the worktrees and builds go; set it to reuse them
                           between runs (default: a temporary directory, removed after)
    BENCHX_E2E_REVISIONS   two tags, "A,B" (default "v2.4.0,v2.4.3")

Run it inside the example's environment (`environment.yml` there, plus pytest): `spin`
and a `python` with pyperf must be on the PATH. The code under test must have the project
file's `provider`, `collectors` and `compare` entries (PR #65 and PR #71); on a checkout
that lacks them, such as `main` before those merge, the test is skipped, as it is on a
machine without the variables.

What it checks, with the clean trees of two tags: the comparison runs to a verdict with
no failed invariant and is not local-only, and the benchmark process of each side
imported the numpy built for that side, with and without a stale PYTHONPATH exported.
A probe suite records `numpy.__file__` on import, so the check does not depend on a
benchmark's numbers.

With both builds in place it also checks, on a checkout made dirty by an untracked file (removed
afterwards), that a dirty `WORKSPACE` is compared and labeled local-only, with the dirty side named
before measuring and marked by `bx head`; and that a checkout git cannot hash (an unreadable
untracked file) is refused before anything runs. If `BENCHX_E2E_SPIN_EXAMPLE` names the `numpy-spin`
example directory, a single `bx bench` run on the dirty checkout is recorded as dirty and `bx head`
marks it, while the same run on the clean checkout is not marked. A benchmark file that is not committed
is compared and labeled local-only, not refused.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import benchx
from conftest import git

NUMPY = os.environ.get("BENCHX_E2E_NUMPY")
EXAMPLE = os.environ.get("BENCHX_E2E_EXAMPLE")
SPIN_EXAMPLE = os.environ.get("BENCHX_E2E_SPIN_EXAMPLE")
PERSISTENT_WORKTREES = os.environ.get("BENCHX_E2E_WORKTREES")
REVISIONS = os.environ.get("BENCHX_E2E_REVISIONS", "v2.4.0,v2.4.3").split(",")

PACKAGE_ROOT = Path(benchx.__file__).resolve().parent.parent  # the code under test, not an installed copy
PROJECT_FILE_ENTRIES = {"provider", "collectors", "compare"}


def missing_project_file_entries():
    schema = PACKAGE_ROOT.parent / "schemas" / "config" / "0.1.0" / "schema.json"
    present = set(json.loads(schema.read_text())["properties"]) if schema.exists() else set()
    return PROJECT_FILE_ENTRIES - present


pytestmark = [
    pytest.mark.skipif(
        not (NUMPY and EXAMPLE and shutil.which("spin") and shutil.which("python")),
        reason="end-to-end test: set BENCHX_E2E_NUMPY and BENCHX_E2E_EXAMPLE, and have spin and python on the PATH"),
    pytest.mark.skipif(
        bool(missing_project_file_entries()),
        reason="the code under test lacks project-file entries (PR #65, PR #71): "
               + ", ".join(sorted(missing_project_file_entries()))),
]

PROBE = textwrap.dedent('''
    import os

    import numpy as np

    with open(os.environ["PROBE_OUT"], "a") as out:
        out.write(f"{np.__version__} {np.__file__}\\n")


    def bench_noop():
        return None
''')


def bx(args, cwd, env):
    code = f"import sys; sys.path.insert(0, {str(PACKAGE_ROOT)!r}); from benchx.cli import main; sys.exit(main())"
    return subprocess.run([sys.executable, "-c", code, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=3600)


@pytest.fixture
def worktrees(tmp_path):
    root = Path(PERSISTENT_WORKTREES) if PERSISTENT_WORKTREES else tmp_path / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    yield root
    if not PERSISTENT_WORKTREES:  # leave no registration behind in the numpy checkout
        for tree in root.iterdir():
            if tree.is_dir():
                subprocess.run(["git", "-C", NUMPY, "worktree", "remove", "--force", str(tree)], capture_output=True)
        subprocess.run(["git", "-C", NUMPY, "worktree", "prune"], capture_output=True)


@pytest.fixture
def project(tmp_path):
    """A scratch copy of the example, in a git repository of its own so that the suite is
    committed, with the probe suite added and the precision cut down."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns("results", "store.parquet", "__pycache__"))
    (project / "bench_probe.py").write_text(PROBE)
    config = json.loads((project / "benchx.conf.json").read_text())
    for level in config["adapter"]["precision"]["repetitions"]["levels"]:
        level["n"] = 2
    config["compare"] = {"profile": "revisions", "rounds": 3}
    (project / "benchx.conf.json").write_text(json.dumps(config, indent=1))
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "scratch copy of the example")
    return project


def run_compare(project, worktrees, run_key, *, numpy_dir=None, targets=None, extra=(), stale_pythonpath=None,
                suite="bench_probe.py"):
    """`bx compare` of two targets (by default `@numpy=A @numpy=B`) with the probe suite; returns the
    finished process, the file the probe writes, and the environment it ran in."""
    probe_out = project / f"{run_key}.probe"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(NUMPY=str(numpy_dir or NUMPY), WORKTREES=str(worktrees), PROBE_OUT=str(probe_out))
    if stale_pythonpath:
        env["PYTHONPATH"] = str(stale_pythonpath)
    targets = targets or [f"@numpy={ref}" for ref in REVISIONS]
    done = bx(["compare", *targets, "--suite", str(project / suite), "--run-key", run_key, "--json",
               *extra], project, env)
    return done, probe_out, env


def compare(project, worktrees, run_key, stale_pythonpath=None):
    """`bx compare @numpy=A @numpy=B` with the probe suite; returns the comparison document and
    the lines the probe wrote."""
    done, probe_out, _ = run_compare(project, worktrees, run_key, stale_pythonpath=stale_pythonpath)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout), probe_out.read_text().splitlines()


def build_of(worktrees, ref):
    (tree,) = [d for d in worktrees.iterdir() if d.name.startswith(f"{ref}-")]
    return tree / "build-install"


def check(doc, probed, worktrees, *, local_only=False):
    assert doc["failed_invariants"] == [] and doc["missing"] == []
    assert doc["local_only"] is local_only  # two tags, clean trees, unless a checkout was made dirty
    assert [u["rounds"] for u in doc["units"]] == [3]
    expected = {ref.removeprefix("v"): build_of(worktrees, ref) for ref in REVISIONS}
    seen = {}
    for line in probed:
        version, path = line.split(" ", 1)
        seen.setdefault(version, set()).add(Path(path))
    assert set(seen) == set(expected)  # every worker imported one of the two numpys, and both were used
    for version, paths in seen.items():
        assert all(p.is_relative_to(expected[version]) for p in paths), (version, paths)


def test_each_side_imports_the_numpy_built_for_it(project, worktrees):
    doc, probed = compare(project, worktrees, "e2e-clean")
    check(doc, probed, worktrees)


def test_a_stale_pythonpath_does_not_change_what_a_side_imports(project, worktrees):
    """The target's directory comes first on sys.path, so a PYTHONPATH naming one build, which
    would make both sides measure that build if the target were the build root, does nothing."""
    doc, probed = compare(project, worktrees, "e2e-prepare")  # makes sure both builds exist
    check(doc, probed, worktrees)
    first = next(build_of(worktrees, REVISIONS[0]).glob("**/site-packages"))
    doc, probed = compare(project, worktrees, "e2e-stale", stale_pythonpath=first)
    check(doc, probed, worktrees)


@pytest.fixture
def built(project, worktrees):
    """Both builds exist (a first, clean comparison makes them); the second revision's checkout."""
    doc, probed = compare(project, worktrees, "e2e-built")
    check(doc, probed, worktrees)
    return build_of(worktrees, REVISIONS[1]).parent


def test_a_dirty_workspace_is_compared_and_labeled_local_only(project, worktrees, built):
    """UC-03 §10: the second revision's checkout, with an uncommitted file, is the contender (`WORKSPACE`)."""
    uncommitted = built / "benchx-e2e-uncommitted.txt"
    uncommitted.write_text("an uncommitted file\n")
    try:
        done, probe_out, env = run_compare(project, worktrees, "e2e-dirty", numpy_dir=built,
                                           targets=[f"@numpy={REVISIONS[0]}", "@numpy=WORKSPACE"])
        assert done.returncode == 0, done.stderr[-3000:]
        doc, probed = json.loads(done.stdout), probe_out.read_text().splitlines()
        head = bx(["head", "--table"], project, env)
    finally:
        uncommitted.unlink()
    check(doc, probed, worktrees, local_only=True)
    # Only the subject is dirty: the probe suite is a clean file in a repository of its own, recorded
    # apart from the checkout, so no benchmark warning, and the clean tag is not named.
    assert {c for u in doc["units"] for c in u["caveats"]} == {"contender: dirty-subject-tree"}
    said = done.stderr
    assert f"contender tree {built}" in said and "local-only" in said
    assert said.index("local-only") < said.index("round 1/")  # said before measuring
    assert head.returncode == 0 and "dirty-subject-tree" in head.stdout


def test_an_uncommitted_benchmark_file_is_compared_and_labeled_local_only(project, worktrees, built):
    """The suite is a file outside both checkouts; one that is not committed is `dirty`, not refused."""
    (project / "bench_uncommitted.py").write_text(PROBE)  # untracked in the project's repository
    done, probe_out, _ = run_compare(project, worktrees, "e2e-suite", suite="bench_uncommitted.py")
    assert done.returncode == 0, done.stderr[-3000:]
    doc, probed = json.loads(done.stdout), probe_out.read_text().splitlines()
    check(doc, probed, worktrees, local_only=True)
    assert {c for u in doc["units"] for c in u["caveats"]} == {"baseline: dirty-benchmark-tree",
                                                                "contender: dirty-benchmark-tree"}
    assert [line.split()[0] for line in done.stderr.splitlines() if "benchmark file" in line] == ["baseline", "contender"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_workspace_git_cannot_hash_is_refused_before_anything_runs(project, worktrees, built):
    """An unreadable untracked file leaves the checkout's dirty state unknown, and with no tree id."""
    unreadable = built / "benchx-e2e-unreadable.txt"
    unreadable.write_text("x\n")
    unreadable.chmod(0)
    try:  # --no-build: the provider only describes, so nothing is built either
        done, _, _ = run_compare(project, worktrees, "e2e-unknown", numpy_dir=built, extra=["--no-build"],
                                 targets=[f"@numpy={REVISIONS[0]}", "@numpy=WORKSPACE"])
    finally:
        unreadable.chmod(0o644)
        unreadable.unlink()
    assert done.returncode == 2, done.stderr[-3000:]
    assert "has no working-tree id" in done.stderr and "unknown" in done.stderr
    assert not (project / "results" / "e2e-unknown").exists()  # no order was written


@pytest.mark.skipif(not SPIN_EXAMPLE, reason="set BENCHX_E2E_SPIN_EXAMPLE to the numpy-spin example directory")
def test_numpy_spin_records_a_dirty_run_and_head_marks_it(tmp_path, built):
    """UC-01 with `numpy-spin`'s own provider: a single run on a dirty checkout is recorded as dirty."""
    spin = tmp_path / "spin"
    shutil.copytree(SPIN_EXAMPLE, spin, ignore=shutil.ignore_patterns("results", "store.parquet", "__pycache__"))
    config = json.loads((spin / "benchx.conf.json").read_text())
    for level in config["adapter"]["precision"]["repetitions"]["levels"]:
        level["n"] = 2
    (spin / "benchx.conf.json").write_text(json.dumps(config, indent=1))
    git(spin, "init", "-q")
    git(spin, "add", "-A")
    git(spin, "commit", "-qm", "scratch copy of the example")
    build = built / "build-install"
    env = dict(os.environ, NUMPY=str(built), BUILD=str(build),
               PYTHONPATH=str(next(build.glob("**/site-packages"))))  # the example's own "hack"

    def run(run_key):
        done = bx(["bench", "@numpy", "--suite", str(spin / "bench_linalg.py"), "--run-key", run_key], spin, env)
        assert done.returncode == 0, done.stderr[-3000:]

    run("spin-clean")
    uncommitted = built / "benchx-e2e-uncommitted.txt"
    uncommitted.write_text("an uncommitted file\n")
    try:
        run("spin-dirty")
    finally:
        uncommitted.unlink()
    rows = bx(["head", "--table"], spin, env).stdout.splitlines()
    dirty, clean = ([r for r in rows if key in r] for key in ("spin-dirty", "spin-clean"))
    # Only the subject is dirty: the suite is a clean file in the example's own repository.
    assert dirty and all(r.rstrip().endswith("success  [dirty-subject-tree]") for r in dirty)
    assert clean and not any("dirty-" in r for r in clean)
    stored = [json.loads(line) for line in bx(["head", "--json"], spin, env).stdout.splitlines() if line.strip()]
    flags = {d["provenance"]["run_key"]: d["provenance"]["subject_dirty"] for d in stored}
    assert flags == {"spin-clean": "clean", "spin-dirty": "dirty"}
