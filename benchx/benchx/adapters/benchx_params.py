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


def expand(name, ids, case_filter):
    """The cases of one benchmark that `case_filter` (a regex, or None for all) selects, as
    (case id, combination index) pairs; the index is None for a benchmark without parameters.

    As in `asv run --bench`, the filter is searched in `name(p0, p1)` once per combination
    of a parametrized benchmark (`ids` are its value strings, one list per parameter), so
    the name alone, with nothing after it, selects every combination, and `name$` none.
    The case id of a combination is `name-INDEX`.
    """
    if not ids:
        return [(name, None)] if not case_filter or re.search(case_filter, name) else []
    return [(f"{name}-{i}", i) for i, combo in enumerate(itertools.product(*ids))
            if not case_filter or re.search(case_filter, f"{name}({', '.join(combo)})")]


def matches_name_only(name, ids, case_filter) -> bool:
    """Whether a filter that selected none of a benchmark's cases matches its name alone:
    a parametrized benchmark is searched in `name(p0, p1)`, so `name$` is a likely slip."""
    return bool(ids and case_filter and re.search(case_filter, name))
