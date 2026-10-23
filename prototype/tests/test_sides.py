"""Resolving one side of a measure-mode comparison (session._side), before anything runs."""

import pytest

from benchx import core, harnesses, session
from conftest import SOURCE_URI, cases, git, make_build

SUITE = "demo-bench"


def description(path, kind="working_tree", **target):
    return {"schema_version": "benchx/target-description/0.1.0",
            "target": {"kind": kind, "path": str(path), "source": {"uri": SOURCE_URI, "type": "git"}, **target}}


def provided(monkeypatch, desc):
    """Stand in for the provider: every @spec resolves to `desc`."""
    monkeypatch.setattr(session.target_mod, "resolve", lambda spec, operation, provider=None: desc)


def executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def tree(tmp_path):
    """A clean git checkout whose suite is in the tree itself."""
    root = tmp_path / "tree"
    root.mkdir()
    git(root, "init", "-q")
    executable(root / SUITE)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tree")
    return root


def test_a_bare_build_directory_is_a_build(repo, tmp_path):
    build = make_build(tmp_path / "b", repo["wt-base"], cases())
    side = session._side(str(build), SUITE, "describe", harness=harnesses.GOOGLE_BENCHMARK)
    assert side["kind"] == "build"
    assert side["path"] == build.resolve()
    assert side["source"] == str(repo["wt-base"].resolve())  # from CMakeCache.txt
    assert side["identity"]["dirty"] == "clean"


def test_a_missing_suite_is_refused_by_the_adapter(repo, tmp_path):
    build = make_build(tmp_path / "b", repo["wt-base"], cases())
    with pytest.raises(session.SessionError, match="suite binary not found"):
        session._side(str(build), "no-such-bench", "describe", harness=harnesses.GOOGLE_BENCHMARK)


def test_a_suite_the_runner_could_not_execute_is_refused_up_front(repo, tmp_path):
    build = make_build(tmp_path / "b", repo["wt-base"], cases())
    (build / "not-executable").write_text("")
    with pytest.raises(session.SessionError, match="suite binary not found"):
        session._side(str(build), "not-executable", "describe", harness=harnesses.GOOGLE_BENCHMARK)


def test_a_provided_working_tree_is_its_own_source(tree, monkeypatch):
    provided(monkeypatch, description(tree))
    side = session._side("@tree", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)
    assert (side["kind"], side["path"], side["source"]) == ("working_tree", tree.resolve(), str(tree.resolve()))
    assert side["identity"]["dirty"] == "clean"


def test_a_working_tree_order_is_valid(tree, monkeypatch):
    provided(monkeypatch, description(tree))
    side = session._side("@tree", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)
    order = session._order(side, harness=harnesses.GOOGLE_BENCHMARK, suite=SUITE, filter=None, quantities=["wall-time"],
                           precision={"repetitions": 3}, environment_variables=None, project=None, run_key="rk",
                           source_uri=SOURCE_URI, round_=0, slot=0, labels=None)
    assert order["target"]["kind"] == "working_tree"
    assert order["target"]["source_dir"] == str(tree.resolve())
    assert core.check(order, "work-order") == []


def test_a_working_tree_outside_git_is_identified_by_content(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    executable(root / SUITE)
    provided(monkeypatch, description(root))
    side = session._side("@plain", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)
    assert side["identity"]["revision"].startswith("directory-")
    assert side["identity"]["dirty"] == "dirty"  # so a revisions comparison refuses it


def test_a_missing_working_tree_says_so(tmp_path, monkeypatch):
    provided(monkeypatch, description(tmp_path / "gone"))
    with pytest.raises(session.SessionError, match="working tree not found"):
        session._side("@gone", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)


def test_an_artifact_target_is_still_deferred(monkeypatch):
    desc = description("x", kind="artifact")
    desc["target"] = {"kind": "artifact", "uri": "https://example.org/a.whl", "sha256": "0" * 64,
                      "source": {"uri": SOURCE_URI, "type": "git"}, "revision": "abc"}
    provided(monkeypatch, desc)
    with pytest.raises(session.SessionError, match="'artifact' is deferred"):
        session._side("@a", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)


def test_the_harness_decides_what_a_suite_is(tree, monkeypatch):
    provided(monkeypatch, description(tree))
    with pytest.raises(session.SessionError, match="suite module not found"):
        session._side("@tree", "benchmarks/missing.py", "prepare", harness=harnesses.PYPERF)


def test_a_dirty_working_tree_is_reported_dirty(tree, monkeypatch):
    (tree / "scratch.txt").write_text("x")
    provided(monkeypatch, description(tree))
    assert session._side("@tree", SUITE, "prepare", harness=harnesses.GOOGLE_BENCHMARK)["identity"]["dirty"] != "clean"


# --- a bare path is read by the harness ---------------------------------------------

@pytest.fixture
def pyperf_tree(tmp_path):
    """A clean git checkout holding a pyperf module."""
    root = tmp_path / "wt"
    (root / "benchmarks").mkdir(parents=True)
    git(root, "init", "-q")
    (root / "benchmarks" / "mod.py").write_text("def bench_a():\n    pass\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "tree")
    return root


def test_the_harness_says_what_a_bare_path_names():
    assert harnesses.GOOGLE_BENCHMARK.BARE_PATH_KIND == "build"
    assert harnesses.PYPERF.BARE_PATH_KIND == "working_tree"


def test_a_bare_path_is_a_working_tree_for_pyperf(pyperf_tree):
    side = session._side(str(pyperf_tree), "benchmarks/mod.py", "describe", harness=harnesses.PYPERF)
    assert (side["kind"], side["path"], side["source"]) == ("working_tree", pyperf_tree.resolve(),
                                                           str(pyperf_tree.resolve()))
    assert side["identity"]["dirty"] == "clean"


def test_a_bare_pyperf_path_is_whole_even_with_a_colon(tmp_path):
    odd = tmp_path / "a:b"
    odd.mkdir()
    with pytest.raises(session.SessionError, match=r"working tree not found: .*a:b"):
        session._side(str(odd / "x"), "mod.py", "describe", harness=harnesses.PYPERF)


def test_a_bare_pyperf_path_without_git_is_identified_by_content(tmp_path):
    root = tmp_path / "plain"
    (root / "benchmarks").mkdir(parents=True)
    (root / "benchmarks" / "mod.py").write_text("def bench_a():\n    pass\n")
    side = session._side(str(root), "benchmarks/mod.py", "describe", harness=harnesses.PYPERF)
    assert side["identity"]["revision"].startswith("directory-")


def test_a_bare_pyperf_path_missing_the_module_is_refused(pyperf_tree):
    with pytest.raises(session.SessionError, match="suite module not found"):
        session._side(str(pyperf_tree), "benchmarks/gone.py", "describe", harness=harnesses.PYPERF)
