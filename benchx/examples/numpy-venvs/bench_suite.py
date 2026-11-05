"""Three small numpy benchmarks. pyperf calls each bench_* function many times and times the
calls, so the arrays are made once, at module level."""

import numpy as np

_rng = np.random.default_rng(0)
_square = _rng.standard_normal((200, 200))
_vector = _rng.standard_normal(100_000)


def bench_matmul():
    _square @ _square


def bench_sort():
    np.sort(_vector)


def bench_sum():
    _vector.sum()
