"""What the runner needs from a harness adapter (harness-adapter.md §1).

An adapter is a module with these names. The runner looks one up by the work
order's `suites[].adapter` and calls nothing else.

Identity and capabilities
    NAME                      the adapter token, as written in `suites[].adapter`
    PRODUCER                  the `producer` object of every result it emits
    QUANTITIES                {quantity name: (native field, quantity declaration)}
    CONTEXT_KEY               the key under which harness-native facts are filed
                              in `observed_context` and `provenance.info`
    WORKLOAD_PARAMETERS       whether the order's `workload_parameters` can be applied

Driving half (harness-adapter.md §5)
    protocol(precision)       -> (applied protocol, invocation): the full protocol
                              that will apply, and what the harness is given to apply it
    locate(target_path, suite, env) -> the thing to run; raises Unsupported if absent
                              or unusable in `env`, the environment it would run in
    list_cases(runnable, case_filter, env) -> the planned cases, fixed before running
    run_case(runnable, case, invocation, env, timeout, native_path) -> the run record;
                              never raises for harness failures, which become results.
                              The record may carry `artifacts`, a list of
                              (kind, media_type, path) the runner records beside
                              stdout, stderr and the native file

Translating half (harness-adapter.md §6)
    attempted(applied)        -> the repetitions the protocol asked for, the denominator
                              of `partial`
    translate(case, quantities, run, attempted) -> one output per quantity
    context_facts(native)     -> (observed context, provenance info) from native output
    harness(info)             -> the `comparison_context.harness` object

Optional hooks (the runner calls them when present)
    comparison_extras(info)   -> more `comparison_context` keys, such as host runtime
    ENV_ALLOWLIST             -> names of environment variables this harness's
                              ecosystem cares about, added to the core default and
                              the project's $BENCHX_ENV_ALLOWLIST. The runner
                              composes the list once and passes it to `run_case`
                              as `env_names` and to `observed_environment`
    observed_environment(run, case, env_names) -> (facts, warnings): facts
                              replace the runner's own `observed_context` keys
                              (None drops one), warnings go to `quality.warnings`.
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

from ..core import Refusal, Rejected


class Unsupported(Rejected):
    """An order this adapter cannot carry out as written; the runner refuses it."""

    def __init__(self, *reasons: str) -> None:
        super().__init__([Refusal(reason) for reason in reasons])
