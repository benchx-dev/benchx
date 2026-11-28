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
import logging
import math
import os
import re
import signal
import subprocess
import textwrap
import time
from pathlib import Path

from .. import log
from ..core import Refusal, Rejected
from .benchx_params import combination, search_name

logger = logging.getLogger(__name__)


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
    return "\n".join(
        [
            f"the filter matches the name of a parametrized benchmark ({names}) but not its arguments.",
            "They are searched as name(p0, p1), each as its repr, in the order of param_names:",
            f"  {first['name']}({', '.join(first['param_names'])})",
            "for example:",
            f"  {search_name(first['name'], first['params'], 0)}",
        ]
    )


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
        raise Unsupported(
            f"cannot read the benchmark listing of {what}: {found}, not the JSON the listing ends with"
        ) from None


# How long a benchmark's manager gets to tidy up after Ctrl-C before its group is killed. pyperf, given Ctrl-C on its
# own, had stopped its worker and exited within a second.
STOP_GRACE_S = 1.0


# After a group is killed, how long to wait for its pipes to reach their end. They do at once, unless something that
# left the group (a daemon, `setsid`) still holds them: then what has arrived is taken, and the rest is not waited for.
KILL_DRAIN_S = 0.5


def kill_group(proc) -> tuple[str, str]:
    """`SIGKILL` to the group `proc` leads (what a benchmark started itself included), reap `proc`, and return what
    its pipes held, stdout and stderr, as text. The wait on the pipes is bounded (`KILL_DRAIN_S`): a plain
    `communicate()` would wait for a process that left the group and holds them, for as long as it lives."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:  # already gone
        pass
    proc.wait()
    try:
        return proc.communicate(timeout=KILL_DRAIN_S)
    except subprocess.TimeoutExpired as holding:
        for pipe in (proc.stdout, proc.stderr):  # what arrived is in the exception, as bytes
            if pipe is not None:
                pipe.close()
        return tuple(
            data.decode(errors="backslashreplace") if isinstance(data, bytes) else data or ""
            for data in (holding.stdout, holding.stderr)
        )


def stop_group(proc, grace=STOP_GRACE_S) -> None:
    """`bx` was interrupted while `proc`, which leads a process group of its own, ran. The terminal's SIGINT reached
    `bx` and not that group, so give the group what the terminal would have: SIGINT, so that pyperf's manager prints
    its "interrupted" message and kills its worker, as it does when it is run alone. Then wait up to `grace` seconds
    (a second Ctrl-C ends the wait), then `kill_group`."""
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:  # already gone
        pass
    try:
        proc.communicate(timeout=grace)  # keeps draining the pipes, so a chatty manager can finish
    except BaseException:  # the grace ran out, or another Ctrl-C: no more waiting
        pass
    kill_group(proc)


def note_cases(adapter_logger, cases, case_filter, limit=40) -> list:
    """What `list_cases` found, for the log: how many, under which filter, and the first `limit` names."""
    shown = cases[:limit]
    more = f", and {len(cases) - len(shown)} more" if len(cases) > len(shown) else ""
    adapter_logger.debug(
        "%d case(s)%s: %s%s",
        len(cases),
        f" for the filter {case_filter!r}" if case_filter else "",
        ", ".join(shown),
        more,
    )
    return cases


def note_parameters(adapter_logger, runnable, limit=40) -> None:
    """The parametrized benchmarks `list_cases` found: each one's parameters with their values (asv's `repr()`
    strings) and the number of combinations, one case each. At most `limit` benchmarks."""
    found = [(name, info) for name, info in runnable.get("benchmarks", {}).items() if info.get("param_names")]
    for name, info in found[:limit]:
        values = ", ".join(
            f"{parameter}: {', '.join(shown[:10])}{', …' if len(shown) > 10 else ''}"
            for parameter, shown in zip(info["param_names"], info["params"])
        )
        adapter_logger.debug(
            "%s: %d combination(s) of %s", name, math.prod(len(shown) for shown in info["params"]), values
        )
    if len(found) > limit:
        adapter_logger.debug("and %d more parametrized benchmark(s)", len(found) - limit)


def note_case(adapter_logger, args, *, cwd, timeout) -> None:
    """The command a `run_case` is about to start, as the log shows it, with the directory it
    runs in (a relative path in the arguments only means something from there) and its limit."""
    adapter_logger.debug("run_case: %s (in %s, limit %g s)", log.command(args), cwd or Path.cwd(), timeout)


def note_case_ended(adapter_logger, status, timeout, native_path, *, parsed, skipped=False) -> None:
    """After `run_case`'s process: a timeout that killed the group, and the native output file, which a
    case that exited 0 should have left (WARNING when it is missing or does not parse). A case that
    failed or timed out has its case line as the warning, so a missing file is then DEBUG; so is the
    file of a benchmark that skipped itself, which has none."""
    if status is None:
        adapter_logger.debug("timed out after %g s: the process group was killed", timeout)
    if not native_path.exists():
        if not skipped:
            adapter_logger.log(
                logging.WARNING if status == 0 else logging.DEBUG, "native output missing: %s", native_path
            )
    elif not parsed:
        adapter_logger.warning("native output does not parse: %s", native_path)


def run_bounded(args, *, timeout, what, scripts=None, temporary=(), **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run(args, capture_output=True, text=True, **kwargs)` with a time limit.

    The child leads its own process group, and the whole group is killed when the limit
    passes (or when the caller is interrupted), so nothing it started outlives it and
    nothing it started keeps the output pipes open. An overrun raises Unsupported, naming
    `what` was being done: an order whose discovery cannot finish cannot be planned.
    `timeout` None means no limit.
    """
    logger.debug(
        "%s: %s (in %s, limit %s)",
        what,
        log.command(args, scripts=scripts, temporary=temporary),
        kwargs.get("cwd") or Path.cwd(),
        f"{timeout:g} s" if timeout else "none",
    )
    started = time.monotonic()
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, **kwargs
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException as error:
        stdout, stderr = kill_group(proc)
        if isinstance(error, subprocess.TimeoutExpired):
            logger.debug("%s: no exit after %.1f s; its process group was killed", what, time.monotonic() - started)
            log.harness_output(logger, what, "stderr", stderr, failed=True)
            log.harness_output(logger, what, "stdout", stdout, failed=True)
            raise Unsupported(f"{what} did not finish within {timeout:g} s (timeouts.order_s)") from None
        logger.debug("%s: interrupted (%s); its process group was killed", what, type(error).__name__)
        raise
    logger.debug("%s: exit %d in %.1f s", what, proc.returncode, time.monotonic() - started)
    # stdout is the listing benchx parses, so it is logged only when the call failed
    log.harness_output(logger, what, "stderr", stderr, failed=proc.returncode != 0)
    if proc.returncode != 0:
        log.harness_output(logger, what, "stdout", stdout, failed=True)
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
