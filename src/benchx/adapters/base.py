"""What the runner needs from a harness adapter (harness-adapter.md §1).

An adapter is a module with these names. The runner looks one up by the work
order's `suites[].adapter` and calls nothing else.

Identity and capabilities
    NAME                      the adapter token, as written in `suites[].adapter`
    PRODUCER                  the `producer` object of every result it emits
    QUANTITIES                {quantity name: (native field, quantity declaration)}
    CONTEXT_KEY               the key under which harness-native facts are filed
                              in `provenance.info`
    WORKLOAD_PARAMETERS       whether the order's `workload_parameters` can be applied

Driving half (harness-adapter.md §5)
    protocol(precision)       -> (applied protocol, invocation): the full protocol
                              that will apply, and what the harness is given to apply it
    locate(target_path, suite, env, timeout=None) -> the thing to run; raises Unsupported
                              if absent or unusable in `env`, the environment it would
                              run in
    list_cases(runnable, case_filter, env, timeout=None) -> the planned cases, fixed
                              before running; the runner refuses an empty plan (an
                              adapter that knows why may raise `no_cases(...)` with
                              more to say). Both run the user's code to find out, so
                              each subprocess they start is limited to `timeout` seconds
                              (the order's `timeouts.order_s`; None is no limit), and
                              an overrun raises Unsupported (see `run_bounded`)
    run_case(runnable, case, invocation, env, timeout, native_path) -> the run record;
                              never raises for harness failures, which become results.
                              The record may carry `artifacts`, a list of
                              (kind, media_type, path) the runner records beside
                              stdout, stderr and the native file

Translating half (harness-adapter.md §6)
    attempted(applied)        -> the repetitions the protocol asked for, the denominator
                              of `partial`
    translate(case, quantities, run, attempted) -> one output per quantity
    context_facts(native)     -> (system facts, provenance info) from native output; the
                              system facts are merged into `environment.system`
    harness(info)             -> the `protocol.harness` object

Optional hooks (the runner calls them when present)
    ENV_ALLOWLIST             -> names of environment variables this harness's
                              ecosystem cares about, added to the core default and
                              the project's $BENCHX_ENV_ALLOWLIST. The runner
                              composes the list once and passes it to `run_case`
                              as `env_names` and to `observed_environment`
    observed_environment(run, case, env_names) -> (facts, warnings): facts are
                              merged over the runner's own `environment.system`
                              (None is null: read but undeterminable), warnings
                              go to `quality.warnings`.
                              For an adapter that can see the environment its
                              harness actually had.
    workload(case, runnable) -> (name, parameters): the workload a case measures,
                              when the harness has structured parameters (ASV's
                              params). Without it the case is the name. The
                              parameters join the order's environment variables
                              in `workload.parameters`. An adapter whose
                              benchmarks take asv-style `params` uses
                              `parametrized_workload` (see there for what its
                              `runnable` holds).
    case_protocol(applied, run, case) -> the protocol this case ran under, for a
                              harness whose benchmarks declare their own precision
                              (ASV's number, repeat, warmup_time). Without it every
                              case reports the order's protocol.
    repetition_levels(applied, run, case) -> `procedure.repetition_levels`: per level,
                              `{unit, attempted, completed}` as totals over the
                              attempt. For a harness that repeats at several levels.
"""

import difflib
import json
import os
import re
import signal
import subprocess
import textwrap

from ..core import Refusal, Rejected
from .benchx_params import combination, search_name


class Unsupported(Rejected):
    """An order this adapter cannot carry out as written; the runner refuses it."""

    def __init__(self, *reasons: str) -> None:
        super().__init__([Refusal(reason) for reason in reasons])


def parametrized_workload(case, runnable) -> tuple[str, dict]:
    """The benchmark name of a case and its parameters as value strings, by parameter name.

    `runnable["cases"]` maps a case id to (benchmark name, combination index or None) and
    `runnable["benchmarks"]` a name to what `list_cases` found: `params` (value strings,
    one list per parameter) and `param_names`. A case id is `name-INDEX` for a parametrized
    benchmark, which another benchmark's own name can look like, so the benchmark is found
    by that table and never by taking the id apart."""
    name, index = runnable["cases"][case]
    if index is None:
        return name, {}
    info = runnable["benchmarks"][name]
    return name, dict(zip(info["param_names"], combination(info["params"], index)))


def name_only_note(found) -> str:
    """For a refusal: the filter is about parametrized benchmarks (`found`, their listing entries)
    but matches none of their arguments (see `benchx_params.matches_name_only`). Says how a case
    is searched, with the first benchmark's parameters, in order, and one case as it is searched."""
    first = found[0]
    names = ", ".join(info["name"] for info in found[:3])
    return "\n".join([
        f"the filter matches the name of a parametrized benchmark ({names}) but not its arguments.",
        "They are searched as name(p0, p1), each as its repr, in the order of param_names:",
        f"  {first['name']}({', '.join(first['param_names'])})",
        "for example:",
        f"  {search_name(first['name'], first['params'], 0)}"])


def similar(case_filter, names, limit=5, cutoff=0.75) -> list[str]:
    """The names a filter may have meant: those that have, as a dotted part (module, class, method), the
    most of its words, or words close to them, then the closest first. A word is a run of letters, digits
    and `_` of three characters or more, so an escape such as `\\d` says nothing, and the case of a letter
    is not a difference. A filter that names a module and misspells its class thus finds that class's
    benchmarks, and not the whole module."""
    words = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", case_filter or "") if len(w) >= 3]
    scored = []
    for name in names:
        parts = name.lower().split(".")
        best = (max(difflib.SequenceMatcher(None, word, part).ratio() for part in parts) for word in words)
        hits = [score for score in best if score >= cutoff]
        if hits:
            scored.append((-len(hits), -sum(hits), name))
    scored.sort()
    return [name for count, _, name in scored[:limit] if count == scored[0][0]]


def no_cases(where, case_filter, available=(), notes=(), limit=5) -> Unsupported:
    """The refusal for a plan with no case in it. Nothing would run, and a filter with a
    typo must not end in `0 result(s)` and a success status. `available` are the names
    that could have been selected (the first `limit` are shown) and `notes` say what else
    the adapter knows about why the plan is empty, each on its own lines (a note may have several).
    The last line is the names that are similar to the filter (`similar`), else the first available.
    The filter is shown as given, not as a Python string, so its backslashes are the ones typed."""
    head = f"filter '{case_filter}' selects no case in {where}" if case_filter else f"no case to run in {where}"
    lines = [head] + [textwrap.indent(note, "  ") for note in notes]
    names = sorted(available)
    close = similar(case_filter, names, limit)
    if close:
        lines.append(f"  similar: {', '.join(close)}")
    elif names:
        more = f", … ({len(names) - limit} more)" if len(names) > limit else ""
        lines.append(f"  available: {', '.join(names[:limit])}{more}")
    return Unsupported("\n".join(lines))


def check_filter(case_filter) -> None:
    """Unsupported when `case_filter` is not a regular expression, before any listing process
    is started: `re.search` would otherwise raise a PatternError, a traceback, after it."""
    if case_filter:
        try:
            re.compile(case_filter)
        except re.error as e:
            raise Unsupported(f"the case filter {case_filter!r} is not a regular expression: {e}") from None


def last_json(out: subprocess.CompletedProcess, what: str):
    """The JSON a listing process prints on its last line of stdout. A module that prints
    nothing, or prints after the listing (an `atexit` handler, say), is a refusal that names
    the line, not a traceback: the process exited 0, so `run_bounded` cannot tell."""
    lines = out.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        found = f"its last line of output is {lines[-1][:80]!r}" if lines else "it printed nothing"
        raise Unsupported(f"cannot read the benchmark listing of {what}: {found}, not the JSON "
                          "the listing ends with") from None


def run_bounded(args, *, timeout, what, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run(args, capture_output=True, text=True, **kwargs)` with a time limit.

    The child leads its own process group, and the whole group is killed when the limit
    passes (or when the caller is interrupted), so nothing it started outlives it and
    nothing it started keeps the output pipes open. An overrun raises Unsupported, naming
    `what` was being done: an order whose discovery cannot finish cannot be planned.
    `timeout` None means no limit.
    """
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException as error:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise Unsupported(f"{what} did not finish within {timeout:g} s (timeouts.order_s)") from None
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
