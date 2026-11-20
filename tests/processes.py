"""Finding processes, for the tests of what `bx` leaves behind when it is stopped (Linux, /proc)."""

import os
import time
from pathlib import Path

HAS_PROC = Path("/proc/self/stat").exists()


def alive(pid: int) -> bool:
    """Running, not a zombie waiting to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] != "Z"


def running(needle: str) -> list[int]:
    """The live processes whose command line contains `needle` (not this one)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if needle in cmdline and alive(int(entry)):
            found.append(int(entry))
    return found


def wait_until(condition, seconds: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


def wait_gone(pids, seconds: float) -> bool:
    return wait_until(lambda: not any(alive(pid) for pid in pids), seconds)
