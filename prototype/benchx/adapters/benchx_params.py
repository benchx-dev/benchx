"""Parametrized benchmarks as asv defines them (harness-adapter.md §4.3, §6.7).

Standalone: the standard library only, so the adapters copy this file beside the
scripts that run in the benchmark's interpreter, which load it by its path under the
module name `benchx_params`. It is named for benchx because those scripts' directory is
on `sys.path`, where a benchmark's own `import params` must not find it.

A benchmark declares `params` (one list of values per parameter, or a single flat list)
and `param_names`. One combination of values is one case, in `itertools.product` order,
and a value is recorded as the string `stable_repr` gives, as asv does.
"""

import itertools
import re
from collections import Counter


def stable_repr(obj) -> str:
    """repr() without a default `at 0x…` address, so the string is stable across runs."""
    text = repr(obj)
    match = re.match(r"^(<.*) at (0x[\da-fA-F]*)(>)$", text)
    if match:
        default = re.match(r"^(<.*) at (0x[\da-fA-F]*)(>)$", object.__repr__(obj))
        if default and default[2] == match[2]:
            text = match[1] + match[3]
    return text


def normalize(params, param_names):
    """(values per parameter, parameter names, value strings per parameter).

    The value strings are what is recorded and matched; the same string twice in one
    parameter is suffixed ` (0)`, ` (1)`, so every combination has its own name.
    """
    params = list(params)
    if params and not isinstance(params[0], (tuple, list)):
        params = [params]
    else:
        params = [list(entry) for entry in params]
    param_names = [str(n) for n in param_names][:len(params)]
    param_names += [f"param{k + 1}" for k in range(len(param_names), len(params))]
    ids = []
    for entry in params:
        entry = [stable_repr(item) for item in entry]
        dupes = {n: 0 for n, count in Counter(entry).items() if count > 1}
        for j, n in enumerate(entry):
            if n in dupes:
                entry[j] = f"{n} ({dupes[n]})"
                dupes[n] += 1
        ids.append(entry)
    return params, param_names, ids


def combination(values, index):
    """The `index`-th combination of `values` (one list per parameter), as a tuple."""
    return next(itertools.islice(itertools.product(*values), index, index + 1))
