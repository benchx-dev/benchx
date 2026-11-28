"""Runner: one work order in, one result per attempt × quantity out.

It changes nothing on the machine (benchmark-environments.md §2). It refuses
an order it cannot carry out exactly, before running anything; every outcome
of a case it did run is a result.
"""

import collections
import contextlib
import json
import logging
import os
import statistics
import time
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, adapters, collectors, core, log, snapshot
from . import target as target_mod
from .adapters.base import Unsupported, no_cases
from .core import Refusal, Rejected
from .projection import said

logger = logging.getLogger(__name__)

RUNNER = {"name": "benchx", "version": __version__}
# The runner's own probe: one condition measurement per attempt (schema §4.4).
PROBE_PRODUCER = {"name": "benchx/runner-probe", "version": __version__, "mapping_version": "benchx-probe/v1"}
LOAD = {"name": "load-average", "unit": "1"}
LOAD_PROBE = {"name": "os.getloadavg", "sensor": "1-minute", "sampling": {"mode": "before-attempt"}}


class Refused(Rejected):
    """The order cannot be carried out as written; nothing was run."""


# Order fields whose feature the implementation does not have. Ignoring one
# would run the order without what it asked for, so the runner refuses it.
DEFERRED = {
    "environment_policy": "enforcing launch settings and verifying requested hardware",
    "build": "a declared build configuration (the runner reads CMakeCache.txt)",
    "components": "pinned components",
    "schedule": "side scheduling (the session loop alternates the sides)",
}


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")


def ingest_key(run_key, workload, parameters, quantity, slot) -> str:
    """harness-adapter.md §4.6, with the slot as the attempt discriminator."""
    variant = _quote(workload) + ("/" + _quote(core.canonical(parameters).decode()) if parameters else "")
    key = f"{_quote(run_key)}:{variant}:{_quote(quantity)}"
    return key if slot is None else f"{key}:slot-{slot}"


def file_name(key: str) -> str:
    """harness-adapter.md §4.1: separators replaced, so files sort by key."""
    return key.replace("/", "_").replace(":", "_") + ".json"


def _subject_name(order) -> str:
    # runner-schema.md has no order-level name override; always derived.
    path = urllib.parse.urlparse(order["target"]["source"]["uri"]).path.rstrip("/")
    return path.rsplit("/", 1)[-1].removesuffix(".git") or "subject"


def _artifact(kind, media_type, path: Path) -> dict:
    return {
        "kind": kind,
        "media_type": media_type,
        "uri": path.resolve().as_uri(),
        "sha256": core.sha256_hex(path.read_bytes()),
    }


def _load_output(load: float | None) -> dict:
    """The load-average sibling of a case's results, read before the case ran:
    a condition measurement (schema §4.4), so tools keep it out of what they
    compare and show."""
    measurement = (
        {"status": "success", "observations": [load]}
        if load is not None
        else {"status": "skipped", "reason": "probe-unavailable"}
    )
    return {"quantity": LOAD, "measurement": {"kind": "condition", **measurement}, "procedure": {}, "info": {}}


def check(order: dict) -> Iterator[Refusal]:
    """Everything in a valid order the implementation cannot apply exactly."""
    if len(order["suites"]) != 1:
        yield Refusal("exactly one suite per order is supported")
    suite = order["suites"][0]
    adapter = adapters.get(suite["adapter"])
    if adapter is None:
        yield Refusal(f"no adapter for {suite['adapter']!r}")
    if order["target"]["kind"] not in ("build", "working_tree"):
        yield Refusal(f"only build and working_tree targets are supported; {order['target']['kind']!r} is deferred")
    if "include" in suite or "exclude" in suite:
        yield Refusal("only a single suites[].filter is supported, not include/exclude")
    if adapter is not None:
        if "workload_parameters" in order and not adapter.WORKLOAD_PARAMETERS:
            yield Refusal(f"{adapter.NAME} takes no workload_parameters")
        unknown = set(order["quantities"]) - set(adapter.QUANTITIES)
        if unknown:
            yield Refusal(f"quantities not supported: {sorted(unknown)}")
    for field, what in DEFERRED.items():
        if field in order:
            yield Refusal(f"{field} is deferred in the implementation: {what}")
    if adapter is not None:
        try:
            adapter.protocol(order["precision"])
        except Unsupported as e:
            yield from e.problems


def _benchmark_mismatches(stated: dict | None, found: dict | None):
    """Refusals when the order's `benchmark` is not what the suite file turns out to be."""
    if stated is None:
        return
    if found is None:
        yield Refusal("the order names a benchmark source, but the suite is in the subject's own checkout")
    elif (stated["source"]["uri"], stated.get("revision")) != (found["uri"], found.get("revision")):
        yield Refusal(
            f"the order names benchmark {stated['source']['uri']}"
            f"{'@' + stated['revision'] if 'revision' in stated else ''}, "
            f"but the suite is in {found['uri']}{'@' + found['revision'] if 'revision' in found else ''}"
        )


def _refuse(problems):
    """Log the whole refusal (the console shows it once the CLI catches it) and raise it: every
    place `run` refuses comes through here."""
    problems = list(problems)
    logger.debug("refused with %d problem(s): %s", len(problems), "; ".join(str(problem) for problem in problems))
    raise Refused(problems) from None


@contextlib.contextmanager
def _partial(state, total, out_dir):
    """A case loop that raises or is interrupted after some result files were written: say what was
    left, and how to store it. The exception goes on."""
    try:
        yield
    except BaseException as error:
        if state["done"]:
            logger.log(
                logging.ERROR if isinstance(error, Exception) else logging.WARNING,
                "%d of %d cases written to %s, not ingested; bx ingest %s stores them",
                state["done"],
                total,
                out_dir,
                out_dir,
            )
        raise


def _overlap(base: dict, facts: dict, path="") -> list[str]:
    """The paths at which `facts` replaces a different value of `base`: names only, never values."""
    found = []
    for key, value in facts.items():
        if key in base:
            if isinstance(value, dict) and isinstance(base[key], dict):
                found += _overlap(base[key], value, f"{path}/{key}")
            elif base[key] != value:
                found.append(f"{path}/{key}")
    return found


def _parameters_text(declared: dict, from_order: dict) -> str:
    """A case's workload parameters for the log: the benchmark's own (asv's `repr()` strings), and the
    order's `-e` variables, which the runner records as workload parameters too. Values as given."""
    parts = [
        "parameters " + ", ".join(f"{name}={value}" for name, value in declared.items())
        if declared
        else "no parameters of its own"
    ]
    if from_order:
        parts.append("environment variables of the order " + ", ".join(f"{n}={v}" for n, v in from_order.items()))
    return "; ".join(parts)


def _path_of(uri) -> str:
    return urllib.parse.unquote(urllib.parse.urlparse(uri).path)


# The log's cap on a stream of harness output: 40 lines, or this many when the harness was asked to say more.
VERBOSE_OUTPUT_LINES = 2000

# Written once into the artifacts folder and overwritten by every case: named on the first case's line only.
SHARED_ARTIFACTS = ("params-helper", "asv-runtime")


def _case_line(results, warnings, run_, duration, load, details=None) -> tuple[int, str, bool]:
    """The case's line: its statuses with their reasons (the message the producer gave, as `bx bench`
    and `bx compare` show it), the warnings that differ from case to case, the exit status, the
    duration and the load average. The level, and whether the case did badly: a status that is not
    `success` or `skipped`, or a non-zero exit, or a warning that says the environment recorded may
    not be the one the workers had."""
    statuses = []
    for status, reason, document in results:
        text = status + (f" ({reason})" if reason else "")
        counts = document.get("procedure", {})
        if status == "partial" and "completed_repetitions" in counts and "attempted_repetitions" in counts:
            text += f" {counts['completed_repetitions']}/{counts['attempted_repetitions']}"
        if text not in statuses:
            statuses.append(text)
    message = next((m for m in (said(document) for _, _, document in results) if m), None)
    line = ", ".join(statuses) + (f": {message}" if message else "")
    if warnings:
        line += f" [{', '.join(f'{w}: {details[w]}' if w in (details or {}) else w for w in warnings)}]"
    ended = "timeout" if run_["timed_out"] else f"exit {run_['exit_status']}"
    bad = (
        any(status not in ("success", "skipped") for status, _, _ in results)
        or run_["timed_out"]
        or run_["exit_status"] not in (0, None)
    )
    raised = bad or bool({"env-mismatch", "env-capture-missing"} & set(warnings))
    load_text = f"load {load:.2f}" if load is not None else "load n/a"
    return (logging.WARNING if raised else logging.INFO), f"{line}, {ended}, {duration:.1f} s, {load_text}", bad


def _log_identity(runnable, source, benchmark, source_dir):
    """What each order found out about where it runs and what it measures. What is the same in every
    order of a command (the interpreter, a dirty tree) is INFO the first time and DEBUG after; the sides of a
    comparison may differ in those, so each distinct one is INFO once."""

    def level(key):
        return logging.INFO if log.once(key) else logging.DEBUG

    python = runnable.get("python") if isinstance(runnable, dict) else None
    if python:
        logger.log(level(("interpreter", str(python))), "the benchmarks run under %s", python)
    snapshot.note_identity_fallback(source)
    method = "directory-hash" if source["revision"].startswith("directory-") else "git"
    logger.debug(
        "subject: %s, found by %s: revision %s, tree %s, %s",
        source_dir,
        method,
        source["revision"][:12],
        (source.get("tree") or "none")[:12],
        source["dirty"],
    )
    if benchmark:
        logger.debug(
            "benchmark: %s, %s: tree %s, %s",
            benchmark["uri"],
            benchmark["type"],
            (benchmark.get("tree") or "none")[:12],
            benchmark["dirty"],
        )
    for what, state, changes, where, in_git in (
        ("subject", source["dirty"], source.get("changes"), source_dir, method == "git"),
        (
            "benchmark",
            benchmark["dirty"] if benchmark else None,
            None,
            benchmark["uri"] if benchmark else None,
            bool(benchmark) and benchmark["type"] == "git",
        ),
    ):
        if state == "dirty":
            shown = (changes or [])[:10]
            more = f", and {len(changes) - len(shown)} more" if changes and len(changes) > len(shown) else ""
            if shown:
                why = f": {'; '.join(shown)}{more}"
            elif in_git:  # a repository whose changes were not listed (the benchmark's are not)
                why = ""
            else:
                why = " (not under git: a directory of files is always dirty)"
            code = "dirty-subject-tree" if what == "subject" else "dirty-benchmark-tree"
            logger.log(
                level(("dirty", what, str(where))), "the %s tree is dirty%s; every result carries %s", what, why, code
            )
        elif state == "unknown":  # accepted here, so a result will say nothing about it: say it now
            logger.warning(
                "the %s tree's dirty state is unknown (%s): its results carry no tree id and no warning",
                what,
                source.get("why_unknown", "git could not say"),
            )
    untracked = source.get("untracked")
    if untracked and log.once(
        ("untracked", source["path"])
    ):  # the identity is the tracked files: say what it leaves out
        shown = untracked[:10]
        more = f", and {len(untracked) - len(shown)} more" if len(untracked) > len(shown) else ""
        logger.info(
            "the subject tree has %d untracked path(s), which its dirty state and tree id leave out (they cover "
            "tracked files only): %s%s; `git add` makes one part of the tree",
            len(untracked),
            ", ".join(shown),
            more,
        )


def _log_records(
    order, adapter, child_env, env_names, environment, system, configuration, setup, source, declared_text
):
    """What the order records, by name (DEBUG, once per order): never a value of the inherited environment."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    from_order = order.get("environment_variables", {})
    extra = {n.strip() for n in child_env.get("BENCHX_ENV_ALLOWLIST", "").split(",") if n.strip()}

    def origin(name):
        return "core" if name in snapshot.ENV_ALLOWLIST else "$BENCHX_ENV_ALLOWLIST" if name in extra else "adapter"

    allowed = [
        f"{n} ({origin(n)}; {'set by the order, enforced' if n in from_order else 'inherited, observed'})"
        for n in env_names
        if n in child_env
    ]
    overriding = sorted(n for n in from_order if n in os.environ)
    kept = set(declared_text) - {"target_provider"} | ({"provider"} if "target_provider" in declared_text else set())
    dropped = [f for f in target_mod.TEXT_FIELDS if f in order["target"] and f not in kept]
    setup_file = Path(child_env.get("BENCHX_SETUP_FILE") or Path(source["path"]) / ".benchx" / "setup.json")
    logger.debug(
        "records: environment %s; system facts %s; allowlisted variables set: %s; the order's variables "
        "that override an inherited one: %s",
        ", ".join(sorted(environment)) or "none",
        ", ".join(sorted(system)) or "none",
        "; ".join(allowed) or "none",
        ", ".join(overriding) or "none",
    )
    logger.debug(
        "records: CMake configuration %s; setup file %s; declared text fields kept %s, dropped as empty %s",
        ", ".join(sorted(configuration)) or "none",
        setup_file if setup else "none read",
        ", ".join(sorted(kept)) or "none",
        ", ".join(dropped) or "none",
    )


def run(order_path, out_dir, allow_collectors=False) -> dict:
    """Execute one order; write results and the order into out_dir. The order's
    context collectors are commands the runner would execute, so they run only
    if `allow_collectors`."""
    inspection = core.inspect(Path(order_path), "work-order")
    if inspection.data is None:
        _refuse(inspection.errors)
    order = inspection.data
    refusals = list(check(order))
    if refusals:
        _refuse(refusals)
    if order.get("collectors") and not allow_collectors:
        _refuse(
            [
                Refusal(
                    "the order names context collectors, which are commands to run; pass --allow-collectors to run it"
                )
            ]
        )
    adapter = adapters.get(order["suites"][0]["adapter"])
    applied_protocol, invocation = adapter.protocol(order["precision"])
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "protocol: %s; invocation: %s",
            json.dumps(applied_protocol, sort_keys=True, default=str),
            json.dumps(invocation, sort_keys=True, default=str),
        )

    target_path = Path(order["target"]["path"]).expanduser()
    child_env = {**os.environ, **order.get("environment_variables", {})}
    try:
        runnable = adapter.locate(
            target_path, order["suites"][0]["suite"], child_env, timeout=order["timeouts"]["order_s"]
        )
    except Unsupported as e:
        refusals += e.problems
    if order["target"]["kind"] == "working_tree":
        # The tree is the code: inspect it as found, dirty or not, never clean or check it out.
        source_dir = order["target"].get("source_dir") or target_path
        source = snapshot.git_identity(source_dir) or snapshot.directory_identity(source_dir)
        configuration = {}
    else:
        source_dir = order["target"].get("source_dir") or snapshot.cmake_source_dir(target_path)
        source = snapshot.git_identity(source_dir) if source_dir else None
        configuration = snapshot.cmake_configuration(target_path)
    if source is None:
        # runner.md §2: a result that cannot state its revision is a defect.
        refusals.append(
            Refusal(
                "cannot locate the checkout the target was built from "
                f"({snapshot.why_no_checkout(target_path, source_dir)}); set target.source_dir"
            )
        )
    benchmark = None
    if source is not None and getattr(adapter, "SUITE_IS_SOURCE", False):
        # The suite's own code, when it is not in the subject's checkout (runner-schema.md §3.3):
        # found as it is, whatever the order says; an order that says otherwise is refused.
        benchmark = snapshot.benchmark_identity(target_path / order["suites"][0]["suite"], source["path"])
        refusals += _benchmark_mismatches(order.get("benchmark"), benchmark)
    elif "benchmark" in order:
        refusals.append(Refusal(f"{adapter.NAME} suites are the subject's own; the order names a benchmark source"))
    if refusals:
        _refuse(refusals)
    _log_identity(runnable, source, benchmark, source_dir)

    parameters = dict(order.get("environment_variables", {}))
    environment = snapshot.environment()
    env_names = snapshot.env_allowlist(child_env, getattr(adapter, "ENV_ALLOWLIST", ()))
    system = snapshot.merge(environment.pop("system"), snapshot.system_facts(child_env, env_names))
    # Declared by the user, recorded as given, never executed or checked (runner-schema.md §3.1).
    declared_text = target_mod.text(order["target"])
    if "provider" in declared_text:
        declared_text["target_provider"] = declared_text.pop("provider")
    setup, setup_warning = snapshot.setup_facts(source["path"], child_env)
    if setup:
        system["setup"] = setup  # declared; no placement rules in the implementation, so matched as a whole
    _log_records(order, adapter, child_env, env_names, environment, system, configuration, setup, source, declared_text)

    out_dir = Path(out_dir)
    ref = core.order_ref(order)
    slot, round_ = order.get("slot"), order.get("round")
    artifacts_dir = out_dir / "artifacts" / (f"slot-{slot}" if slot is not None else ref[7:19])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    collected = collectors.run_all(
        order.get("collectors", []),
        declared=order["target"].get("how_built"),
        cwd=target_path,
        env=child_env,
        artifacts_dir=artifacts_dir,
        taken=system,
    )
    if collected.refusals:
        _refuse(collected.refusals)
    system.update(collected.system)
    order_file = out_dir / f"workorder-{ref[7:]}.json"
    order_file.write_bytes(core.pretty(order))

    attempted = adapter.attempted(applied_protocol)
    harness_verbosity = order.get("harness_verbosity", 0)
    if harness_verbosity:
        logger.info(
            "the harness is asked to say more (harness_verbosity %d): its output is logged up to %d lines a stream",
            harness_verbosity,
            VERBOSE_OUTPUT_LINES,
        )
    logger.info("listing the benchmarks of %s", order["suites"][0]["suite"])  # an asv listing imports a whole suite
    listing_started = time.monotonic()
    try:
        cases = adapter.list_cases(
            runnable, order["suites"][0].get("filter"), child_env, timeout=order["timeouts"]["order_s"]
        )
    except Unsupported as e:
        _refuse(e.problems)
    logger.info("listed %d case(s) in %.1f s", len(cases), time.monotonic() - listing_started)
    if not cases:
        _refuse(no_cases(order["suites"][0]["suite"], order["suites"][0].get("filter")).problems)
    workloads = [adapter.workload(case, runnable) if hasattr(adapter, "workload") else (case, {}) for case in cases]
    # Both share workload.parameters: a clash would overwrite the case's own value and merge distinct cases.
    clashes = sorted({name for _, case_parameters in workloads for name in case_parameters} & parameters.keys())
    if clashes:
        _refuse([Refusal(f"environment variables named like workload parameters: {', '.join(clashes)}; rename them")])
    logger.info("order %s: %s, %d case(s)", ref[:19], adapter.NAME, len(cases))
    written, overwritten, counts, named_once = [], [], collections.Counter(), set()
    state, order_started, nonzero = {"done": 0}, time.monotonic(), False
    with _partial(state, len(cases), out_dir):
        for index, case in enumerate(cases):
            stem = f"{index:04d}"
            native_path = artifacts_dir / f"{stem}.native.json"
            label = f"case {index + 1}/{len(cases)} {case}"
            # before the clock starts: nothing is logged while a case runs
            logger.info("%s: starting, limit %g s", label, order["timeouts"]["case_s"])
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "%s: workload %s; %s", label, workloads[index][0], _parameters_text(workloads[index][1], parameters)
                )
            load = snapshot.load_average()
            # The wall clock can step backwards mid-case (NTP, WSL2 resyncs): the duration comes from a monotonic clock.
            started, clock = time.time(), time.monotonic()
            run_ = adapter.run_case(
                runnable,
                case,
                invocation,
                child_env,
                order["timeouts"]["case_s"],
                native_path,
                env_names=env_names,
                **({"harness_verbosity": harness_verbosity} if harness_verbosity else {}),
            )
            duration = time.monotonic() - clock
            (artifacts_dir / f"{stem}.stdout").write_text(run_["stdout"])
            (artifacts_dir / f"{stem}.stderr").write_text(run_["stderr"])
            artifacts = [
                _artifact("stdout", "text/plain", artifacts_dir / f"{stem}.stdout"),
                _artifact("stderr", "text/plain", artifacts_dir / f"{stem}.stderr"),
            ]
            if native_path.exists():
                artifacts.insert(0, _artifact("native-output", "application/json", native_path))
            artifacts += [_artifact(kind, media_type, path) for kind, media_type, path in run_.get("artifacts", [])]
            artifacts += [_artifact(kind, media_type, path) for kind, media_type, path in collected.artifacts]
            native_system, native_info = adapter.context_facts(run_["native"])
            scaling = native_system.get("cpu", {}).get("scaling_enabled") is True
            debug_build = native_info.get("library_build_type") == "debug"
            # recorded in the result, flagged nowhere else; each condition once, so that a side with one does not
            # spend the warning of a side with the other
            noisy = [
                text
                for flag, key, text in (
                    (scaling, "scaling", "CPU frequency scaling is enabled"),
                    (debug_build, "debug", "the benchmark library is a debug build"),
                )
                if flag and log.once(("noisy-context", key))
            ]
            if noisy:
                logger.warning("%s: the harness's own context says %s", label, " and ".join(noisy))
            case_system, case_warnings = snapshot.merge(system, native_system), []
            if hasattr(adapter, "observed_environment"):
                facts, case_warnings = adapter.observed_environment(run_, case, env_names)
                case_system = snapshot.merge(case_system, facts)
            if log.once("overlap") and (clash := _overlap(system, case_system)):
                # what the harness reported replaces what the runner read (snapshot.merge: the harness wins)
                logger.debug("the harness's facts replace the context's at: %s", ", ".join(clash))
            attempt_key = f"urn:benchx:attempt:{ref[7:23]}:{index}"
            workload_name, case_parameters = workloads[index]
            case_parameters = {**case_parameters, **parameters}
            # A harness whose benchmarks declare their own precision reports each case's own protocol.
            case_protocol = (
                adapter.case_protocol(applied_protocol, run_, case)
                if hasattr(adapter, "case_protocol")
                else applied_protocol
            )
            protocol = {**case_protocol, "harness": adapter.harness(native_info)}

            results, case_paths = [], []
            for output in [*adapter.translate(case, order["quantities"], run_, attempted), _load_output(load)]:
                condition = output["quantity"] is LOAD
                procedure = dict(output["procedure"])
                if not condition:
                    procedure["duration_seconds"] = round(duration, 6)
                if round_ is not None:
                    procedure["round"] = round_
                if slot is not None:
                    procedure["slot"] = slot
                if not condition:
                    procedure["timeout_seconds"] = order["timeouts"]["case_s"]
                if hasattr(adapter, "repetition_levels") and not condition:
                    procedure["repetition_levels"] = adapter.repetition_levels(applied_protocol, run_, case)
                info = {"workorder_ref": ref, "requester": order["requester"], **output["info"]}
                if harness_verbosity:  # the output between samples is the harness's own: say that it was asked for
                    info["harness_verbosity"] = harness_verbosity
                if native_info:
                    info[adapter.CONTEXT_KEY] = native_info
                if "reason" in order:
                    info["reason"] = order["reason"]
                info.update(declared_text)
                provenance = {
                    "run_key": order["run_key"],
                    "started_at": _timestamp(started),
                    "ended_at": _timestamp(started + duration),
                    "subject_dirty": source["dirty"],
                    "benchmark_dirty": benchmark["dirty"] if benchmark else source["dirty"],
                    "runner": RUNNER,
                    **({} if condition else {"artifacts": artifacts}),
                    "info": info,
                }
                if "tree" in source:
                    provenance["subject_tree"] = provenance["benchmark_tree"] = source["tree"]
                if benchmark:
                    provenance["benchmark_tree"] = benchmark["tree"]
                if "labels" in order:
                    provenance["labels"] = order["labels"]
                workload = {"name": workload_name}
                if case_parameters:
                    workload["parameters"] = case_parameters
                if len(case_parameters) > 1:  # the store sorts keys: the order is data (projection.declared_workload)
                    info["parameter_names"] = list(case_parameters)
                subject = {"name": _subject_name(order), "components": [{"role": "primary"}]}
                if configuration:
                    subject["configuration"] = configuration
                key = ingest_key(order["run_key"], workload_name, case_parameters, output["quantity"]["name"], slot)
                document = {
                    "schema_version": core.catalog.MEASUREMENT_RESULT,
                    "producer": PROBE_PRODUCER if condition else adapter.PRODUCER,
                    "ingest_key": key,
                    "attempt_key": attempt_key,
                    **({"project": order["project"]} if "project" in order else {}),
                    "source": order["target"]["source"],
                    "revision": {"key": source["revision"]},
                    "benchmark": (
                        {
                            "source": {"uri": benchmark["uri"], "type": benchmark["type"]},
                            **({"revision": {"key": benchmark["revision"]}} if "revision" in benchmark else {}),
                        }
                        if benchmark
                        else {
                            "source": order["target"]["source"],
                            # An unversioned tree is identified by its hash in provenance, not by a revision.
                            **(
                                {}
                                if source["revision"].startswith("directory-")
                                else {"revision": {"key": source["revision"]}}
                            ),
                        }
                    ),
                    "coordinates": {
                        "workload": workload,
                        "subject": subject,
                        # A condition measurement's protocol is its probe alone (schema §4.4).
                        "protocol": {"probe": LOAD_PROBE} if condition else protocol,
                        "environment": {**environment, "system": case_system},
                    },
                    "measurement": {"quantity": output["quantity"], **output["measurement"]},
                    "procedure": procedure,
                    "provenance": provenance,
                }
                # A result says so when its subject or benchmark tree is dirty (schema §4.1).
                dirty = [code for flag, code in snapshot.DIRTY_WARNINGS if provenance[flag] == "dirty"]
                warnings = (
                    []
                    if condition
                    else (dirty + ([setup_warning] if setup_warning else []) + collected.warnings + case_warnings)
                )
                if warnings:
                    document["quality"] = {"warnings": warnings}
                errors = core.check(document, "measurement-result")
                if errors:
                    # harness-adapter.md §4.1: never drop a case, never emit an invalid document.
                    document["measurement"] = {
                        "quantity": output["quantity"],
                        **({"kind": "condition"} if condition else {}),
                        "status": "error",
                        "reason": "adapter.mapping-failed",
                    }
                    document["provenance"]["info"]["mapping_error"] = str(errors[0])
                    logger.warning(
                        "%s: result %s (%s) fell back to adapter.mapping-failed: %s",
                        label,
                        key,
                        output["quantity"]["name"],
                        "; ".join(str(error) for error in errors[:5]),
                    )
                    document["procedure"] = {
                        k: v
                        for k, v in procedure.items()
                        if k not in ("inner_iterations", "completed_repetitions", "repetition_levels")
                    }
                    errors = core.check(document, "measurement-result")
                    if errors:
                        raise RuntimeError(f"the error result is itself invalid: {errors[0]}")
                path = out_dir / file_name(key)
                if (
                    path.exists()
                ):  # a rerun of a run key: the store will refuse these results, and the folder says otherwise
                    overwritten.append(path)
                    if len(overwritten) == 1:
                        logger.warning(
                            "%s: result file %s exists and is overwritten, and so are the case's files under %s",
                            label,
                            path,
                            artifacts_dir,
                        )
                path.write_bytes(core.pretty(document))
                written.append(path)
                case_paths.append(path)
                if not condition:
                    results.append((document["measurement"]["status"], document["measurement"].get("reason"), document))
            details = (
                adapter.explain_warnings(run_, case, case_warnings) if hasattr(adapter, "explain_warnings") else {}
            )
            if "setup-per-call-unbatched" in case_warnings:
                observations = next(
                    (d["measurement"]["observations"] for _, _, d in results if d["measurement"].get("observations")),
                    None,
                )
                values = [
                    o["value"] if isinstance(o, dict) else o for o in observations or []
                ]  # as the comparator reads them
                if values:
                    details["setup-per-call-unbatched"] = (
                        f"median {statistics.median(values):.3g} s, minimum {min(values):.3g} s"
                    )
            level, line, bad = _case_line(results, case_warnings, run_, duration, load, details)
            logger.log(level, "%s: %s", label, line)
            if logger.isEnabledFor(logging.DEBUG):
                shown = [
                    f"{a['kind']} {_path_of(a['uri'])}"
                    for a in artifacts
                    if a["kind"] not in SHARED_ARTIFACTS or a["kind"] not in named_once
                ]
                named_once.update(a["kind"] for a in artifacts if a["kind"] in SHARED_ARTIFACTS)
                logger.debug(
                    "%s files: %s, results %d %s",
                    label,
                    ", ".join(shown),
                    len(case_paths),
                    case_paths[0] if case_paths else "",
                )
            cap = VERBOSE_OUTPUT_LINES if harness_verbosity else 40
            log.harness_output(
                logger, label, "stdout", run_["stdout"], failed=bad, kept_at=artifacts_dir / f"{stem}.stdout", limit=cap
            )
            log.harness_output(
                logger, label, "stderr", run_["stderr"], failed=bad, kept_at=artifacts_dir / f"{stem}.stderr", limit=cap
            )
            counts.update(status for status, _, _ in results)
            nonzero = nonzero or run_["exit_status"] not in (0, None)
            state["done"] += 1
    took = time.monotonic() - order_started
    logger.log(
        logging.WARNING if {"error", "partial"} & set(counts) or nonzero else logging.INFO,
        "order %s: %d case(s), results: %s, %.1f s",
        ref[:19],
        len(cases),
        ", ".join(f"{status} {n}" for status, n in sorted(counts.items())) or "none",
        took,
    )
    if overwritten:
        logger.warning(
            "order %s: %d result file(s) in %s were overwritten by a rerun of run key %s; the store keeps "
            "the first results of a key and slot and refuses these",
            ref[:19],
            len(overwritten),
            out_dir,
            order["run_key"],
        )
    return {
        "order": ref,
        "cases": len(cases),
        "results": len(written),
        "out": str(out_dir),
        "files": [order_file, *written],
    }
