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
                              in `workload.parameters`.
    case_protocol(applied, run, case) -> the protocol this case ran under, for a
                              harness whose benchmarks declare their own precision
                              (ASV's number, repeat, warmup_time). Without it every
                              case reports the order's protocol.
    repetition_levels(applied, run, case) -> `procedure.repetition_levels`: per level,
                              `{unit, attempted, completed}` as totals over the
                              attempt. For a harness that repeats at several levels.
"""

import os
import signal
import subprocess

from ..core import Refusal, Rejected


class Unsupported(Rejected):
    """An order this adapter cannot carry out as written; the runner refuses it."""

    def __init__(self, *reasons: str) -> None:
        super().__init__([Refusal(reason) for reason in reasons])


def no_cases(where, case_filter, available=(), notes=(), limit=10) -> Unsupported:
    """The refusal for a plan with no case in it. Nothing would run, and a filter with a
    typo must not end in `0 result(s)` and a success status. `available` are the names
    that could have been selected (the first `limit` are shown) and `notes` say what else
    the adapter knows about why the plan is empty."""
    head = f"filter {case_filter!r} selects no case in {where}" if case_filter else f"no case to run in {where}"
    parts = [head]
    names = sorted(available)
    if names:
        more = f", … ({len(names) - limit} more)" if len(names) > limit else ""
        parts.append(f"available: {', '.join(names[:limit])}{more}")
    return Unsupported("; ".join(parts + list(notes)))


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
