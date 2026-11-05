"""Example benchmark suite: plain Python functions, one case each.

`bx bench bench_suite.py` runs every function named bench_* or benchmark_*.
Name functions on the command line to run only those:
    bx bench bench_suite.py bench_sorted bench_join

Each function is the timed body; pyperf calls it many times. Do your setup
at module level (it runs once per worker process, outside the timing).
"""

import os

DATA = list(range(2000, 0, -1))
WORDS = [f"word{i}" for i in range(500)]


def bench_sorted():
    sorted(DATA)


def bench_join():
    ",".join(WORDS)


def bench_dict_build():
    {w: len(w) for w in WORDS}


def bench_sum_squares():
    sum(i * i for i in range(1000))


def bench_env_var():
    """Reads EXAMPLE_N from the environment, to show `-e NAME=VALUE` reaching the workers."""
    int(os.environ.get("EXAMPLE_N", "100")) ** 2


def helper_not_a_case():
    """Not named bench_*, so only run if you name it explicitly."""
