"""Fixtures: a git source, prepared build directories, orders, and rounds.

A build directory is what a user would hand benchx: a CMakeCache.txt, a
compiler record, and a benchmark binary, here the fake from fake_gbench.py.
"""

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from benchx import runner, snapshot
from benchx.core.catalog import SCHEMAS

EXAMPLES = SCHEMAS / "measurement-result" / "0.1.0" / "examples"

FAKE = Path(__file__).with_name("fake_gbench.py")
SOURCE_URI = "https://example.org/demo.git"
QUIET, NOISY = "BM_Quiet/1024", "BM_Noisy/1024"


def git(cwd, *args) -> str:
    out = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                         cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A source with two commits, and a worktree at each."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    (src / "bench.cpp").write_text("// work = 100\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "base")
    base = git(src, "rev-parse", "HEAD")
    (src / "bench.cpp").write_text("// work = 102\n")
    git(src, "commit", "-qam", "head")
    head = git(src, "rev-parse", "HEAD")
    for name, sha in (("wt-base", base), ("wt-head", head)):
        git(src, "worktree", "add", "-q", "--detach", str(tmp_path / name), sha)
    return {"src": src, "base": base, "head": head,
            "wt-base": tmp_path / "wt-base", "wt-head": tmp_path / "wt-head"}


def make_build(path: Path, source: Path, cases: dict, hardened=False, **config) -> Path:
    path.mkdir(parents=True)
    (path / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={source}\n"
        "CMAKE_BUILD_TYPE:STRING=Release\n"
        "CMAKE_CXX_FLAGS:STRING=-O2\n"
        f"CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n"
        "CMAKE_PROJECT_NAME:STATIC=demo\n"
        f"DEMO_HARDENED:BOOL={'ON' if hardened else 'OFF'}\n"
        f"DEMO_DATA_DIR:PATH={source}/data\n")
    compiler = path / "CMakeFiles" / "3.30.0" / "CMakeCXXCompiler.cmake"
    compiler.parent.mkdir(parents=True)
    compiler.write_text('set(CMAKE_CXX_COMPILER_ID "AppleClang")\nset(CMAKE_CXX_COMPILER_VERSION "21.0.0")\n')
    binary = path / "demo-bench"
    binary.write_text(f"#!{sys.executable}\n" + FAKE.read_text())
    binary.chmod(0o755)
    # Each directory draws its own noise, as two real binaries would.
    (path / "fake-bench.json").write_text(json.dumps({"seed": path.name, "cases": cases, **config}))
    return path


def cases(quiet=1.0e-3, noisy=2.0e-3):
    return {QUIET: {"mean": quiet, "rel_sd": 0.002}, NOISY: {"mean": noisy, "rel_sd": 0.08}}


def order(build: Path, source: Path | None, run_key: str, **extra) -> dict:
    target = {"kind": "build", "path": str(build), "source": {"uri": SOURCE_URI, "type": "git"}}
    if source is not None:
        target["source_dir"] = str(source)
    case_s = extra.pop("timeout_seconds", 60)
    document = {
        "schema_version": "benchx/work-order/0.1.0",
        "work_order_id": f"urn:uuid:{uuid.uuid4()}",
        "state": "resolved",
        "suites": [{"adapter": "google-benchmark", "suite": "demo-bench"}],
        "target": target,
        "quantities": ["wall-time"],
        "precision": {"repetitions": 5},
        "timeouts": {"case_s": case_s, "order_s": 600},
        "run_key": run_key,
        "requester": {"kind": "workbench", "name": "tests"},
        # Decorative: the runner does not yet cross-check plan against the
        # runtime-discovered case list (runner-schema.md §3.2, still open).
        "plan": [{"id": "p0", "case": "*", "quantity": "wall-time"}],
    }
    labels = extra.pop("labels", None)
    if labels:
        document["labels"] = labels
    document.update(extra)
    return document


def run_rounds(tmp: Path, sides: list[dict], rounds: int, run_key: str, out: Path, **extra) -> Path:
    """Alternate the sides and assign round and slot, writing result files only.

    Unlike benchx.session (the real loop, which delivers to a store and
    compares), this lets a test put arbitrary fields in the orders.
    """
    slot = 0
    for r in range(rounds):
        for side in sides:
            path = tmp / f"order-{run_key}-{slot}.json"
            path.write_text(json.dumps(order(side["build"], side.get("source"), run_key, round=r, slot=slot,
                                             labels=side.get("labels"), **extra)))
            runner.run(path, out)
            slot += 1
    return out


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No test reads a setup file, and each runs in its own project root: with no
    benchx.conf.json the working directory is the root, so the store and the results
    land in the test's directory."""
    monkeypatch.delenv("BENCHX_SETUP_FILE", raising=False)
    monkeypatch.delenv("BENCHX_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    snapshot.ignore()
    yield
    snapshot.ignore()


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


def copy_tree(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")


# Documents for the core tests: the schema's own examples, and a file writer.


@pytest.fixture(params=sorted(EXAMPLES.glob("*.json")), ids=lambda path: path.stem)
def example(request) -> dict:
    return json.loads(request.param.read_text())


@pytest.fixture
def adhoc() -> dict:
    return json.loads((EXAMPLES / "adhoc.json").read_text())


@pytest.fixture
def arrow() -> dict:
    return json.loads((EXAMPLES / "arrow.json").read_text())


@pytest.fixture
def write(tmp_path):
    def _write(content: str | bytes | dict) -> Path:
        path = tmp_path / "doc.json"
        if isinstance(content, dict):
            content = json.dumps(content)
        if isinstance(content, str):
            content = content.encode()
        path.write_bytes(content)
        return path

    return _write
