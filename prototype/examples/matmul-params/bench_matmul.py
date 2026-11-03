"""Example: one benchmark function, six cases (3 sizes x 2 array libraries).

    bx bench bench_matmul.py                       # every combination
    bx bench bench_matmul.py bench_matmul          # the same, by name
    bx bench bench_matmul.py "bench_matmul(100, 'torch')"   # one combination

`params` holds one list of values per argument of the function and `param_names` names
them; each combination is its own case. Values are recorded as their repr, so the
library is given by name and imported when first used. Both libraries use float64
(torch would default to float32).
"""

import functools
import importlib


@functools.cache
def operands(n, xp):
    lib = importlib.import_module(xp)
    return lib.ones((n, n), dtype=lib.float64), lib.ones((n, n), dtype=lib.float64)


def bench_matmul(n, xp):
    a, b = operands(n, xp)
    a @ b


bench_matmul.params = [[10, 50, 100], ["numpy", "torch"]]
bench_matmul.param_names = ["n", "xp"]
