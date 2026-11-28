"""What makes a tree dirty: a change to a tracked file, and nothing else (schema §4.1).

The flag and the tree id cover the tracked files. A checkout with only untracked files is `clean`, and its
tree id is HEAD's own; the untracked paths are listed in the identity, for the log to name."""

import os

import pytest

from benchx import snapshot
from conftest import git


@pytest.fixture(autouse=True)
def nothing_ignored():
    snapshot.ignore()
    yield
    snapshot.ignore()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "wt"
    (root / "pkg").mkdir(parents=True)
    git(root, "init", "-q")
    (root / ".gitignore").write_text("*.log\n")
    (root / "bench.py").write_text("x = 1\n")
    (root / "pkg" / "m.py").write_text("y = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def head_tree(root):
    return git(root, "rev-parse", "HEAD^{tree}")


def test_a_clean_checkout_has_the_tree_id_of_its_head(checkout):
    identity = snapshot.git_identity(checkout)
    assert (identity["dirty"], identity["tree"]) == ("clean", head_tree(checkout))
    assert "untracked" not in identity and "changes" not in identity


def test_untracked_files_leave_the_checkout_clean_and_are_listed(checkout):
    (checkout / "notes.txt").write_text("scratch\n")
    (checkout / "scratch" / "deep").mkdir(parents=True)
    (checkout / "scratch" / "deep" / "a.py").write_text("1\n")
    (checkout / "scratch" / "deep" / "b.py").write_text("2\n")
    identity = snapshot.git_identity(checkout)
    assert (identity["dirty"], identity["tree"]) == ("clean", head_tree(checkout))
    assert identity["untracked"] == ["notes.txt", "scratch/"]  # a directory once, not file by file


def test_what_git_ignores_and_benchx_writes_is_not_listed(checkout):
    (checkout / "debug.log").write_text("x\n")  # in .gitignore
    (checkout / "pkg" / "__pycache__").mkdir()
    (checkout / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\0")
    (checkout / "out" / "call-1").mkdir(parents=True)
    (checkout / "out" / "call-1" / "result.json").write_text("{}\n")
    identity = snapshot.git_identity(checkout)
    assert identity["dirty"] == "clean" and "untracked" not in identity


def test_a_change_to_a_tracked_file_makes_the_checkout_dirty_with_a_tree_no_commit_has(checkout):
    (checkout / "notes.txt").write_text("untracked: not part of it\n")
    (checkout / "bench.py").write_text("x = 2\n")
    identity = snapshot.git_identity(checkout)
    assert identity["dirty"] == "dirty" and identity["tree"] != head_tree(checkout)
    assert identity["changes"] == [" M bench.py"]
    # The tree is what a commit of the tracked files would carry: committing them reproduces the id.
    git(checkout, "commit", "-qam", "edit")
    assert git(checkout, "rev-parse", "HEAD^{tree}") == identity["tree"]


@pytest.mark.parametrize(
    "change, status",
    [
        (lambda root: (root / "bench.py").unlink(), " D bench.py"),
        (lambda root: (root / "bench.py").chmod(0o755), " M bench.py"),
        (lambda root: git(root, "mv", "bench.py", "renamed.py"), "R  bench.py -> renamed.py"),
        (lambda root: ((root / "new.py").write_text("z = 1\n"), git(root, "add", "new.py")), "A  new.py"),
        (lambda root: git(root, "rm", "-q", "--cached", "bench.py"), "D  bench.py"),
    ],
    ids=["deleted", "mode", "renamed", "staged_new", "untracked_by_rm_cached"],
)
def test_a_deleted_a_chmodded_a_renamed_and_a_staged_file_are_changes(checkout, change, status):
    change(checkout)
    identity = snapshot.git_identity(checkout)
    assert identity["dirty"] == "dirty" and identity["changes"] == [status]
    assert identity["tree"] != head_tree(checkout)
    # Committing the change reproduces the id: what is staged is part of the tree, as `git mv` and `git add` leave it.
    git(checkout, "commit", "-qam", "change")
    assert git(checkout, "rev-parse", "HEAD^{tree}") == identity["tree"]


def test_two_untracked_states_have_one_tree_id_and_two_edits_have_two(checkout):
    (checkout / "a.txt").write_text("1\n")
    first = snapshot.git_identity(checkout)["tree"]
    (checkout / "b.txt").write_text("2\n")
    assert snapshot.git_identity(checkout)["tree"] == first  # a tree that grows files during a run stays one tree
    (checkout / "bench.py").write_text("x = 3\n")
    edited = snapshot.git_identity(checkout)["tree"]
    (checkout / "bench.py").write_text("x = 4\n")
    assert snapshot.git_identity(checkout)["tree"] not in (first, edited)


@pytest.fixture
def with_submodule(checkout, tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "a.txt").write_text("1\n")
    git(inner, "add", "-A")
    git(inner, "commit", "-qm", "one")
    git(checkout, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "third_party")
    git(checkout, "commit", "-qm", "add the submodule")
    return checkout


def test_an_untracked_file_in_a_submodule_is_not_a_change(with_submodule):
    (with_submodule / "third_party" / "scratch.txt").write_text("x\n")
    identity = snapshot.git_identity(with_submodule)
    assert (identity["dirty"], identity["tree"]) == ("clean", head_tree(with_submodule))


def test_an_edit_in_a_submodule_is_dirty_but_its_tree_id_cannot_show_it(with_submodule):
    """The limit schema §4.1 states: a submodule contributes its HEAD commit id, and nothing inside it."""
    (with_submodule / "third_party" / "a.txt").write_text("2\n")
    identity = snapshot.git_identity(with_submodule)
    assert identity["dirty"] == "dirty" and identity["changes"] == [" M third_party"]
    assert identity["tree"] == head_tree(with_submodule)


def test_a_submodule_at_another_commit_changes_the_tree_id(with_submodule):
    inner = with_submodule / "third_party"
    (inner / "a.txt").write_text("2\n")
    git(inner, "commit", "-qam", "two")
    identity = snapshot.git_identity(with_submodule)
    assert identity["dirty"] == "dirty" and identity["tree"] != head_tree(with_submodule)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_tracked_file_leaves_the_state_unknown_and_an_unreadable_untracked_one_does_not(checkout):
    secret = checkout / "secret.txt"
    secret.write_text("x\n")
    secret.chmod(0)
    try:
        assert snapshot.git_identity(checkout)["dirty"] == "clean"
        tracked = checkout / "bench.py"
        tracked.chmod(0)
        try:
            identity = snapshot.git_identity(checkout)
            assert identity["dirty"] == "unknown" and "tree" not in identity
        finally:
            tracked.chmod(0o644)
    finally:
        secret.chmod(0o644)
