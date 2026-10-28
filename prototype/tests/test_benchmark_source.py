"""A suite outside the subject's checkout is its own source, identified by its file (runner-schema.md §3.3)."""

import json
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("pyperf")

from benchx import adapters, cli, core, runner, snapshot
from benchx.store import Store
from conftest import git

SUITE = textwrap.dedent('''
    def bench_a():
        sum(range(100))
''')
RESULTS = Path("results")  # the project root is the test directory
FAST = ["--processes", "2", "--values", "2", "--loops", "20"]


@pytest.fixture
def subject(tmp):
    """The thing measured: a git checkout with no suite of its own."""
    path = tmp / "subject"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", "https://example.org/subject.git")
    (path / "lib.py").write_text("X = 1\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "one")
    return path


@pytest.fixture
def loose_suite(tmp):
    """A suite file in a folder under no version control."""
    path = tmp / "loose"
    path.mkdir()
    (path / "bench_x.py").write_text(SUITE)
    return path / "bench_x.py"


@pytest.fixture
def versioned_suite(tmp):
    path = tmp / "suites"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", "https://example.org/suites.git")
    (path / "bench_x.py").write_text(SUITE)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "suite")
    return path / "bench_x.py"


def bench(subject, suite, run_key, *extra):
    assert cli.main(["bench", str(subject), "--adapter", "pyperf", "--suite", str(suite), *FAST,
                     "--run-key", run_key, *extra]) == cli.EXIT_OK
    return json.loads(next(p for p in sorted((RESULTS / run_key / "orders").glob("*.json"))).read_text())


def result(run_key):
    path = next(p for p in sorted((RESULTS / run_key).glob("*_wall-time.json")))
    document = json.loads(path.read_text())
    assert core.check(document, "measurement-result") == []
    return document


@pytest.mark.parametrize("adapter, apart", [("pyperf", True), ("asv", True), ("google-benchmark", False)])
def test_which_adapters_record_a_suite_apart_from_the_subject(adapter, apart):
    """A suite that is Python source can live outside the subject's tree, and is then recorded as its own
    benchmark; a built binary is the subject's (schema §5.5, `one-benchmark-tree`)."""
    assert adapters.get(adapter).SUITE_IS_SOURCE is apart


def test_a_suite_inside_the_subject_is_the_subjects_own(subject):
    assert snapshot.benchmark_identity(subject / "lib.py", subject) is None


def test_an_unversioned_suite_is_identified_by_its_file(subject, loose_suite):
    found = snapshot.benchmark_identity(loose_suite, subject)
    assert found == {"uri": loose_suite.parent.as_uri(), "type": "directory", "tree": snapshot.blob_id(loose_suite),
                     "dirty": "dirty"}
    assert found["tree"] == git(loose_suite.parent, "hash-object", loose_suite.name)  # git's own blob id


def test_a_versioned_suite_is_clean_until_it_is_edited(subject, versioned_suite):
    clean = snapshot.benchmark_identity(versioned_suite, subject)
    assert (clean["uri"], clean["type"], clean["dirty"]) == ("https://example.org/suites.git", "git", "clean")
    assert clean["revision"] == git(versioned_suite.parent, "rev-parse", "HEAD")
    versioned_suite.write_text(SUITE + "\n# edited\n")
    edited = snapshot.benchmark_identity(versioned_suite, subject)
    assert (edited["revision"], edited["dirty"]) == (clean["revision"], "dirty")
    assert edited["tree"] != clean["tree"]


def test_a_new_file_in_a_repository_is_dirty(subject, versioned_suite):
    new = versioned_suite.parent / "bench_new.py"
    new.write_text(SUITE)
    assert snapshot.benchmark_identity(new, subject)["dirty"] == "dirty"


def test_an_unversioned_suite_is_recorded_without_a_revision(subject, loose_suite):
    order = bench(subject, loose_suite, "u1")
    assert order["benchmark"] == {"source": {"uri": loose_suite.parent.as_uri(), "type": "directory"}}
    assert core.check(order, "work-order") == []
    document = result("u1")
    assert document["benchmark"] == {"source": {"uri": loose_suite.parent.as_uri(), "type": "directory"}}
    assert document["source"]["uri"] == "https://example.org/subject.git"  # the subject is unchanged
    assert document["revision"]["key"] == git(subject, "rev-parse", "HEAD")
    provenance = document["provenance"]
    assert (provenance["benchmark_dirty"], provenance["benchmark_tree"]) == ("dirty", snapshot.blob_id(loose_suite))
    assert provenance["subject_dirty"] == "clean" and provenance["subject_tree"] != provenance["benchmark_tree"]


def test_a_versioned_suite_is_recorded_with_its_own_revision(subject, versioned_suite):
    bench(subject, versioned_suite, "v1")
    document = result("v1")
    assert document["benchmark"]["source"]["uri"] == "https://example.org/suites.git"
    assert document["benchmark"]["revision"]["key"] == git(versioned_suite.parent, "rev-parse", "HEAD")
    assert document["provenance"]["benchmark_dirty"] == "clean"
    assert document["revision"]["key"] == git(subject, "rev-parse", "HEAD")


def test_editing_the_suite_changes_its_tree_but_not_the_series(subject, loose_suite):
    bench(subject, loose_suite, "e1")
    before = result("e1")
    loose_suite.write_text(SUITE + "\n# edited\n")
    bench(subject, loose_suite, "e2")
    after = result("e2")
    assert before["provenance"]["benchmark_tree"] != after["provenance"]["benchmark_tree"]
    series = Store("store.parquet").series(quantity="wall-time")
    assert len(series) == 1


def test_a_suite_inside_the_subject_is_recorded_as_before(subject):
    (subject / "bench_in.py").write_text(SUITE)
    git(subject, "add", "-A")
    git(subject, "commit", "-qm", "suite")
    order = bench(subject, "bench_in.py", "i1")
    assert "benchmark" not in order
    document = result("i1")
    assert document["benchmark"] == {"source": document["source"], "revision": document["revision"]}
    assert document["provenance"]["benchmark_tree"] == document["provenance"]["subject_tree"]


def test_an_order_naming_the_wrong_benchmark_is_refused(subject, loose_suite, tmp):
    order = bench(subject, loose_suite, "m1")
    order["benchmark"] = {"source": {"uri": "https://example.org/elsewhere.git", "type": "git"}, "revision": "abc"}
    path = tmp / "wrong.json"
    path.write_bytes(core.pretty(order))
    with pytest.raises(runner.Refused, match="names benchmark https://example.org/elsewhere.git"):
        runner.run(path, tmp / "out")
