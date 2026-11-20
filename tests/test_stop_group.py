"""`adapters.base.stop_group`: what `bx` does to a benchmark's process group when Ctrl-C reaches `bx` (INTERRUPT_PLAN.md).

The group is a session of its own, so the terminal's SIGINT does not reach it. The children here are small scripts
that start a grandchild, so that what survives can be seen."""

import os
import signal
import subprocess
import sys
import time

import pytest

from benchx.adapters.base import stop_group
from processes import HAS_PROC, alive, wait_gone, wait_until

pytestmark = pytest.mark.skipif(not HAS_PROC, reason="needs /proc")

MANAGER = '''
import os, signal, subprocess, sys, time
pidfile, mode = sys.argv[1], sys.argv[2]
if mode == "ignore":  # the disposition is inherited by the grandchild
    signal.signal(signal.SIGINT, signal.SIG_IGN)
elif mode == "clean":  # as pyperf's manager does: say so, and go
    def bye(*_):
        open(pidfile + ".cleaned", "w").write("yes")
        sys.exit(1)
    signal.signal(signal.SIGINT, bye)
grandchild = subprocess.Popen(["sleep", "60"], **({"start_new_session": True} if mode == "escape" else {}))
open(pidfile, "w").write(f"{os.getpid()} {grandchild.pid}")
print("partial output", flush=True)
time.sleep(60)
'''


def start(tmp_path, mode):
    script = tmp_path / "manager.py"
    script.write_text(MANAGER)
    pidfile = tmp_path / "pids"
    proc = subprocess.Popen([sys.executable, str(script), str(pidfile), mode], text=True, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert wait_until(lambda: pidfile.exists() and pidfile.read_text().count(" ") == 1, 10)
    manager, grandchild = map(int, pidfile.read_text().split())
    return proc, manager, grandchild, pidfile


def test_the_group_is_asked_to_stop_first_as_a_terminal_would(tmp_path):
    proc, manager, grandchild, pidfile = start(tmp_path, "clean")
    stop_group(proc)
    assert (tmp_path / "pids.cleaned").read_text() == "yes"  # its own handler ran: it was given SIGINT, not killed
    assert wait_gone([manager, grandchild], 2)  # and what was left of the group is gone


def test_a_group_that_ignores_sigint_is_killed_when_the_grace_is_over(tmp_path):
    proc, manager, grandchild, _ = start(tmp_path, "ignore")
    started = time.monotonic()
    stop_group(proc, grace=0.4)
    assert 0.3 < time.monotonic() - started < 3
    assert wait_gone([manager, grandchild], 2) and proc.returncode == -signal.SIGKILL


def test_a_second_ctrl_c_ends_the_wait_at_once(tmp_path):
    proc, manager, grandchild, _ = start(tmp_path, "ignore")
    timer = signal.setitimer(signal.ITIMER_REAL, 0.3)  # a second Ctrl-C, 0.3 s into a grace of 20
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    started = time.monotonic()
    try:
        stop_group(proc, grace=20)
    finally:
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 3
    assert wait_gone([manager, grandchild], 2)


def test_a_process_that_left_the_group_and_holds_the_pipes_does_not_hold_bx(tmp_path):
    proc, manager, grandchild, _ = start(tmp_path, "escape")  # the grandchild has a session of its own, and our pipes
    started = time.monotonic()
    stop_group(proc, grace=0.3)
    assert time.monotonic() - started < 3  # not the 60 s the grandchild sleeps
    assert not alive(manager) and alive(grandchild)  # it is outside the group: not ours to kill here
    os.kill(grandchild, signal.SIGKILL)


def test_a_group_that_is_already_gone_is_not_an_error(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    proc.wait()
    stop_group(proc)
