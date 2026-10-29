"""The config schema, its recommended examples, and the paths benchx keeps out of a tree's identity."""

import json
from pathlib import Path

import pytest

from benchx import adapters, core, snapshot
from benchx.core import catalog
from conftest import git

EXAMPLES = sorted((catalog.SCHEMAS / "config" / "0.1.0" / "examples").glob("*.json"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_examples_fit_the_schema(path):
    assert core.check(json.loads(path.read_text()), "config") == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_recommended_settings_are_ones_the_adapter_applies(path):
    adapter_block = json.loads(path.read_text())["adapter"]
    adapter = adapters.get(adapter_block["name"])
    assert core.check_precision(adapter_block["precision"]) == []
    adapter.protocol(adapter_block["precision"])  # raises Unsupported for a shape it cannot apply
    assert set(adapter_block["quantities"]) <= set(adapter.QUANTITIES)


@pytest.mark.parametrize("config, message", [
    ({"adapter": {"suite": "s"}}, "name"),
    ({"adapter": {"name": "pyperf", "quantities": []}}, "quantities"),
    ({"adapter": {"name": "pyperf", "quantities": ["wall-time", "wall-time"]}}, "quantities"),
    ({"provider": {"command": []}}, "command"),
    ({"provider": {"command": "spin"}}, "command"),
    ({"compare": {"profile": "history"}}, "profile"),
    ({"compare": {"rounds": 0}}, "rounds"),
    ({"paths": {"store": ""}}, "store"),
    ({"project": ""}, "project"),
    ({"adapters": {}}, "adapters"),
])
def test_schema_refuses(config, message):
    errors = core.check({"schema_version": "benchx/config/0.1.0", **config}, "config")
    assert errors and message in " ".join(str(e) for e in errors)


# --- paths benchx writes inside a measured tree -----------------------------------

@pytest.fixture(autouse=True)
def _no_ignored_paths():
    snapshot.ignore()
    yield
    snapshot.ignore()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    git(root, "init", "-q")
    (root / "bench.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "m")
    return root


def write_outputs(root):
    (root / "results").mkdir(exist_ok=True)
    (root / "results" / "r.json").write_text("{}")
    (root / "store.parquet").write_bytes(b"PAR1")


def test_a_runs_own_output_marks_the_tree_dirty_unless_ignored(checkout):
    clean = snapshot.git_identity(checkout)
    assert clean["dirty"] == "clean"
    write_outputs(checkout)
    assert snapshot.git_identity(checkout)["dirty"] == "dirty"
    snapshot.ignore(checkout / "results", checkout / "store.parquet")
    again = snapshot.git_identity(checkout)
    assert (again["dirty"], again["tree"]) == ("clean", clean["tree"])


def test_ignoring_does_not_hide_a_real_change(checkout):
    snapshot.ignore(checkout / "results")
    write_outputs(checkout)
    (checkout / "bench.py").write_text("x = 2\n")
    assert snapshot.git_identity(checkout)["dirty"] == "dirty"


def test_a_path_outside_the_tree_changes_nothing(checkout, tmp_path):
    snapshot.ignore(tmp_path / "elsewhere" / "results")
    assert snapshot.git_identity(checkout)["dirty"] == "clean"


def test_a_plain_directory_tree_id_skips_ignored_paths(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "bench.py").write_text("x = 1\n")
    before = snapshot.directory_identity(root)["tree"]
    write_outputs(root)
    assert snapshot.directory_identity(root)["tree"] != before
    snapshot.ignore(root / "results", root / "store.parquet")
    assert snapshot.directory_identity(root)["tree"] == before


def test_a_results_directory_the_project_git_ignores_still_gives_a_tree_id(checkout):
    """Excluding a path that .gitignore already ignores used to make `git add` fail, so the tree id was unknown."""
    (checkout / ".gitignore").write_text("results/\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-qm", "ignore results")
    clean = snapshot.git_identity(checkout)
    write_outputs(checkout)
    snapshot.ignore(checkout / "results", checkout / "store.parquet")
    again = snapshot.git_identity(checkout)
    assert (again["dirty"], again["tree"]) == ("clean", clean["tree"])
    snapshot.ignore(checkout / "results")  # the store file is untracked and not ignored: a real difference
    assert snapshot.git_identity(checkout)["dirty"] == "dirty"
