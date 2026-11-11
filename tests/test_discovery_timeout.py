"""Discovery before the cases is bounded by `timeouts.order_s`.

Probing the interpreter and listing the benchmarks run the user's code, so a module that
never finishes importing, or a binary that never answers, must end in a refusal and not a
hang. The limits here are fractions of a second; the code under test sleeps for a minute.
"""

import contextlib
import json
import os
import sys
import time

import pytest

from benchx import cli, runner, session
from benchx.adapters import asv, base, pyperf
from conftest import SOURCE_URI, cases, make_build, order

LIMIT = 0.5  # seconds: an artificially small timeouts.order_s
SLEEPS_A_MINUTE = "#!/bin/sh\nsleep 60\n"
SLEEPING_PYTHON = f"#!{sys.executable}\nimport time\ntime.sleep(60)\n"
NEVER_IMPORTED = "import time\ntime.sleep(60)\n\n\ndef bench_f():\n    pass\n"


@contextlib.contextmanager
def finishes_within(seconds):
    """The block must end long before the minute the code under test would take."""
    start = time.monotonic()
    yield
    assert time.monotonic() - start < seconds, "the limit did not end the wait"


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture
def slow_python(tmp):
    """A directory whose `python` never answers, for the PATH a benchmark would run with."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    executable(bin_dir / "python", SLEEPING_PYTHON)
    return bin_dir


@pytest.fixture
def tree(tmp):
    """A pyperf working tree whose only module never finishes importing."""
    path = tmp / "tree"
    path.mkdir()
    (path / "mod.py").write_text(NEVER_IMPORTED)
    return path


def pyperf_order(tree, **extra):
    document = order(tree, None, "slow", **extra)
    document["target"] = {"kind": "working_tree", "path": str(tree), "source_dir": str(tree),
                          "source": {"uri": SOURCE_URI, "type": "git"}}
    document["suites"] = [{"adapter": "pyperf", "suite": "mod.py"}]
    document["precision"] = {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 1},
                                                                          {"unit": "value", "n": 1}]},
                             "calibration": {"mode": "fixed", "n_iterations": 1}, "warmup": {"mode": "none"}}
    return document


# --- the helper --------------------------------------------------------------------

def test_run_bounded_returns_what_a_finished_child_said():
    out = base.run_bounded([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
                           timeout=30, what="a child")
    assert (out.returncode, out.stdout, out.stderr) == (3, "out\n", "err\n")
    assert base.run_bounded([sys.executable, "-c", "pass"], timeout=None, what="a child").returncode == 0


def test_run_bounded_stops_the_whole_process_group():
    """Killing only the shell leaves its `sleep` holding the output pipe, and waiting for the
    pipe to close would take the minute the limit exists to avoid."""
    with finishes_within(20), pytest.raises(base.Unsupported) as raised:
        base.run_bounded(["/bin/sh", "-c", "sleep 60; echo done"], timeout=LIMIT, what="a shell")
    assert str(raised.value) == "a shell did not finish within 0.5 s (timeouts.order_s)"


# --- Google Benchmark: the binary is asked for its tests ---------------------------------

def test_a_binary_that_never_lists_its_benchmarks_is_a_refusal(repo, tmp, capsys):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    executable(build / "demo-bench", SLEEPS_A_MINUTE)
    path = tmp / "o.json"
    path.write_text(json.dumps(order(build, repo["wt-head"], "slow", timeouts={"case_s": 60, "order_s": LIMIT})))
    message = "listing the benchmarks of demo-bench did not finish within 0.5 s (timeouts.order_s)"
    with finishes_within(20), pytest.raises(runner.Refused) as raised:
        runner.run(path, tmp / "out")
    assert message in str(raised.value)
    assert [p for p in (tmp / "out").glob("*.json") if not p.name.startswith("workorder-")] == []
    with finishes_within(20):
        assert cli.main(["run", str(path), "--out", str(tmp / "out")]) == cli.EXIT_REFUSED
    assert f"refused: {message}" in capsys.readouterr().err


# --- pyperf and asv: the interpreter is probed, and the modules are imported -----------------

@pytest.mark.parametrize("adapter, suite", [(pyperf, "mod.py"), (asv, "benchmarks")])
def test_a_python_that_never_answers_is_a_refusal(tmp, tree, slow_python, adapter, suite):
    (tree / "benchmarks").mkdir()
    with finishes_within(20), pytest.raises(base.Unsupported, match=r"importing pyperf with .*python did not finish "
                                                                      r"within 0\.5 s \(timeouts\.order_s\)"):
        adapter.locate(tree, suite, {"PATH": str(slow_python)}, timeout=LIMIT)


def test_pyperf_stops_importing_a_module_that_never_finishes(tree):
    pytest.importorskip("pyperf")
    runnable = pyperf.locate(tree, "mod.py", dict(os.environ))
    with finishes_within(20), pytest.raises(base.Unsupported, match=r"importing mod\.py to list its benchmarks did not "
                                                                      r"finish within 0\.5 s"):
        pyperf.list_cases(runnable, None, dict(os.environ), timeout=LIMIT)


def test_asv_stops_importing_a_module_that_never_finishes(tmp):
    pytest.importorskip("pyperf")
    package = tmp / "tree" / "benchmarks"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "slow.py").write_text(NEVER_IMPORTED)
    runnable = asv.locate(tmp / "tree", "benchmarks", dict(os.environ))
    with finishes_within(20), pytest.raises(base.Unsupported, match=r"listing the asv benchmarks in .*benchmarks did "
                                                                      r"not finish within 0\.5 s"):
        asv.list_cases(runnable, None, dict(os.environ), timeout=LIMIT)


def test_the_runner_gives_pyperf_the_orders_limit(tmp, tree):
    """End to end: the limit is the order's `timeouts.order_s`, here 1.5 s (the interpreter
    probe must fit in it too, so it is not as small as in the tests above)."""
    pytest.importorskip("pyperf")
    path = tmp / "o.json"
    path.write_text(json.dumps(pyperf_order(tree, timeouts={"case_s": 60, "order_s": 1.5})))
    with finishes_within(40), pytest.raises(runner.Refused, match=r"did not finish within 1\.5 s \(timeouts\.order_s\)"):
        runner.run(path, tmp / "out")


# --- the session probes a side before any round ----------------------------------------------

def test_the_session_probe_is_bounded_too(tmp, tree, slow_python, monkeypatch):
    monkeypatch.setenv("PATH", f"{slow_python}{os.pathsep}{os.environ['PATH']}")
    with finishes_within(20), pytest.raises(session.SessionError, match=r"did not finish within 0\.5 s"):
        session.bench_target(str(tree), suite="mod.py", adapter="pyperf", order_s=LIMIT)
