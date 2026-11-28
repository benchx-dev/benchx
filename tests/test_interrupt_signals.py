"""A real `bx` that is interrupted with Ctrl-C leaves no benchmark running.

`bx` runs in a subprocess, of its own session, on a benchmark slow enough that the run is still
going when the signal comes. Before `bx` stopped its children itself, pyperf's manager and worker
went on until their next write to the pipes `bx` had closed: up to one worker process.
"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import benchx
from conftest import cases, git, make_build
from processes import HAS_PROC, running, wait_gone, wait_until

pytestmark = pytest.mark.skipif(not HAS_PROC, reason="needs /proc")

BX = [sys.executable, "-c", "import sys; from benchx import cli; sys.exit(cli.main(sys.argv[1:]))"]
SRC = str(Path(benchx.__file__).resolve().parent.parent)
LEFT_BY = 4  # seconds `bx` may take, after the signal, to have stopped its children (a grace of 1 s)


def start_bx(cwd, *args):
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.Popen(
        [*BX, *args],
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=open(Path(cwd) / "bx.err", "w"),
    )


def send(bx, signum):
    """Ctrl-C reaches the whole foreground group of `bx`, which is its own session here: not a benchmark's."""
    os.killpg(bx.pid, signum)


def interrupted_run(cwd, needle, signum, *args):
    """Start `bx`, wait for a benchmark process of the run to exist, signal `bx`, and return its exit
    status and whether the benchmark processes were gone `LEFT_BY` seconds after the signal."""
    bx = start_bx(cwd, *args)
    try:
        assert wait_until(lambda: running(needle), 60), (
            f"the benchmark never started; bx said: {(Path(cwd) / 'bx.err').read_text()[-400:]!r}"
        )
        started = running(needle)
        assert bx.pid not in started  # the needle names the benchmark, not bx's own command line
        send(bx, signum)
        status = bx.wait(timeout=20)
        return status, wait_gone(started, LEFT_BY), running(needle)
    finally:
        if bx.poll() is None:
            bx.kill()
        for pid in running(needle):  # whatever a failing test leaves
            os.kill(pid, signal.SIGKILL)


@pytest.fixture
def pyperf_needed():
    pytest.importorskip("pyperf")


@pytest.mark.parametrize("signum", [signal.SIGINT], ids=lambda s: s.name)
def test_a_stopped_bx_leaves_no_pyperf_benchmark_running(tmp_path, pyperf_needed, signum):
    (tmp_path / "slow.py").write_text("import time\n\n\ndef bench_slow():\n    time.sleep(2)\n")
    out = tmp_path / "out"
    status, gone, left = interrupted_run(
        tmp_path,
        f"{out}/artifacts/",
        signum,
        "bench",
        "slow.py",
        "bench_slow",
        "--no-ingest",
        "--out",
        str(out),
        "--processes",
        "3",
        "--values",
        "3",
        "--loops",
        "1",
        "--warmups",
        "0",
    )
    assert status == -signum  # it left as an unhandled signal would
    assert gone, f"benchmark processes still running {LEFT_BY} s after {signum.name}: {left}"


def test_a_stopped_bx_leaves_no_asv_benchmark_running(tmp_path, pyperf_needed):
    suite = tmp_path / "proj"
    (suite / "benchmarks").mkdir(parents=True)
    (suite / "asv.conf.json").write_text('{"version": 1, "project": "slowproj", "benchmark_dir": "benchmarks"}\n')
    (suite / "benchmarks" / "__init__.py").write_text("")
    (suite / "benchmarks" / "bm.py").write_text(
        "import time\n\n\nclass Slow:\n    def time_slow(self):\n        time.sleep(2)\n"
    )
    out = tmp_path / "out"
    status, gone, left = interrupted_run(
        suite,
        f"{out}/artifacts/",
        signal.SIGINT,
        "bench",
        ".",
        "--adapter",
        "asv",
        "--no-ingest",
        "--out",
        str(out),
        "--processes",
        "3",
        "--values",
        "3",
        "--warmups",
        "0",
        "--min-time",
        "0.001",
    )
    assert status == -signal.SIGINT
    assert gone, f"benchmark processes still running {LEFT_BY} s after SIGINT: {left}"


@pytest.mark.parametrize("signum", [signal.SIGINT], ids=lambda s: s.name)
def test_a_stopped_bx_leaves_no_google_benchmark_running(tmp_path, signum):
    source = tmp_path / "src"
    source.mkdir()
    git(source, "init", "-q")
    (source / "bench.cpp").write_text("// work\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "c")
    build = make_build(tmp_path / "build", source, cases(), sleep={"BM_Slow": 60})
    out = tmp_path / "out"
    status, gone, left = interrupted_run(
        tmp_path,
        f"{build}/demo-bench --benchmark_filter",
        signum,
        "bench",
        str(build),
        "--adapter",
        "google-benchmark",
        "--suite",
        "demo-bench",
        "BM_Slow",
        "--no-ingest",
        "--out",
        str(out),
    )
    assert status == -signum
    assert gone, f"benchmark processes still running {LEFT_BY} s after {signum.name}: {left}"
