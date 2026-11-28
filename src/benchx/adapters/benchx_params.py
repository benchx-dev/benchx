"""Parametrized benchmarks as asv defines them (harness-adapter.md §4.3, §6.7).

Standalone: the standard library only, so the adapters copy this file beside the
scripts that run in the benchmark's interpreter, which load it by its path under the
module name `benchx_params`. It is named for benchx because those scripts' directory is
on `sys.path`, where a benchmark's own `import params` must not find it.

A benchmark declares `params` (one list of values per parameter, or a single flat list)
and `param_names`. One combination of values is one case, in `itertools.product` order,
and a value is recorded as the string `stable_repr` gives, as asv does.
"""

import hashlib
import inspect
import itertools
import re
import textwrap
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


def source_version(func):
    """The version of a benchmark function, as asv has it: an explicit `version` attribute (any value,
    kept as a string), else a SHA-256 of its source text, which is what asv_runner hashes for a plain
    function: the lines stripped of trailing whitespace, dedented. Comments and docstrings count. What the
    function calls, its `params` and the library under test do not. None when the source cannot be read
    (a built-in, code made by `exec`) and no version is declared."""
    declared = getattr(func, "version", None)
    if declared is not None:
        return str(declared)
    try:
        lines, _ = inspect.getsourcelines(func)
    except (OSError, TypeError):
        return None
    source = textwrap.dedent("\n".join(line.rstrip() for line in lines)).rstrip()
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


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
    param_names = [str(n) for n in param_names][: len(params)]
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


def _search_text(name, combo) -> str:
    """The text a case filter is searched in for one combination: `name(p0, p1)`."""
    return f"{name}({', '.join(combo)})"


def search_name(name, ids, index) -> str:
    """The text a case filter is searched in: `name(p0, p1)` for the `index`-th combination of a
    parametrized benchmark (`ids` are its value strings, one list per parameter), else the name.
    Selecting a case by this text, exactly, is how a caller picks that one case (see `expand`)."""
    return name if index is None else _search_text(name, combination(ids, index))


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
    return [
        (f"{name}-{i}", i)
        for i, combo in enumerate(itertools.product(*ids))
        if not case_filter or re.search(case_filter, _search_text(name, combo))
    ]


def matches_name_only(name, ids, case_filter) -> bool:
    """Whether a filter that selected none of a benchmark's cases is about this benchmark, though
    not about any of its combinations: it matches the name alone (`name$` is a likely slip), or the
    part of it before its first `\\(` does, so what it gives after the name, the arguments, is
    what fails (in the wrong order, or a value the benchmark does not have)."""
    if not (ids and case_filter):
        return False
    named = case_filter.split(r"\(")[0]
    if not named.strip("^"):  # it starts with its arguments: no name to go by
        return False
    try:
        return bool(re.search(named, name))
    except re.error:
        return False
