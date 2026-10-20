"""Runner: one work order in, one result per attempt × quantity out.

It changes nothing on the machine (benchmark-environments.md §2). It refuses
an order it cannot carry out exactly, before running anything; every outcome
of a case it did run is a result.
"""

import os
import time
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, adapters, collectors, core, snapshot
from . import target as target_mod
from .adapters.base import Unsupported
from .core import Refusal, Rejected

RUNNER = {"name": "benchx-prototype", "version": __version__}
# The runner's own probe: one condition measurement per attempt (schema §4.4).
PROBE_PRODUCER = {"name": "benchx/runner-probe", "version": __version__, "mapping_version": "benchx-probe/v1"}
LOAD = {"name": "load-average", "unit": "1"}
LOAD_PROBE = {"name": "os.getloadavg", "sensor": "1-minute", "sampling": {"mode": "before-attempt"}}


class Refused(Rejected):
    """The order cannot be carried out as written; nothing was run."""


# Order fields whose feature the prototype does not implement. Ignoring one
# would run the order without what it asked for, so the runner refuses it.
DEFERRED = {
    "environment_policy": "enforcing launch settings and verifying requested hardware",
    "build": "a declared build configuration (the runner reads CMakeCache.txt)",
    "components": "pinned components",
    "schedule": "side scheduling (the session loop alternates the sides)",
}


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
    return {"kind": kind, "media_type": media_type, "uri": path.resolve().as_uri(),
            "sha256": core.sha256_hex(path.read_bytes())}


def _load_output(load: float | None) -> dict:
    """The load-average sibling of a case's results, read before the case ran:
    a condition measurement (schema §4.4), so tools keep it out of what they
    compare and show."""
    measurement = ({"status": "success", "observations": [load]} if load is not None
                   else {"status": "skipped", "reason": "probe-unavailable"})
    return {"quantity": LOAD, "measurement": {"kind": "condition", **measurement}, "procedure": {}, "info": {}}


def check(order: dict) -> Iterator[Refusal]:
    """Everything in a valid order the prototype cannot apply exactly."""
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
            yield Refusal(f"{field} is deferred in the prototype: {what}")
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
        yield Refusal(f"the order names benchmark {stated['source']['uri']}"
                      f"{'@' + stated['revision'] if 'revision' in stated else ''}, "
                      f"but the suite is in {found['uri']}{'@' + found['revision'] if 'revision' in found else ''}")


def run(order_path, out_dir, allow_collectors=False) -> dict:
    """Execute one order; write results and the order into out_dir. The order's
    context collectors are commands the runner would execute, so they run only
    if `allow_collectors`."""
    inspection = core.inspect(Path(order_path), "work-order")
    if inspection.data is None:
        raise Refused(inspection.errors)
    order = inspection.data
    refusals = list(check(order))
    if refusals:
        raise Refused(refusals)
    if order.get("collectors") and not allow_collectors:
        raise Refused([Refusal("the order names context collectors, which are commands to run; "
                               "pass --allow-collectors to run it")])
    adapter = adapters.get(order["suites"][0]["adapter"])
    applied_protocol, invocation = adapter.protocol(order["precision"])

    target_path = Path(order["target"]["path"]).expanduser()
    child_env = {**os.environ, **order.get("environment_variables", {})}
    try:
        runnable = adapter.locate(target_path, order["suites"][0]["suite"], child_env)
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
        refusals.append(Refusal("cannot locate the checkout the target was built from; set target.source_dir"))
    benchmark = None
    if source is not None and getattr(adapter, "SUITE_IS_SOURCE", False):
        # The suite's own code, when it is not in the subject's checkout (runner-schema.md §3.3):
        # found as it is, whatever the order says; an order that says otherwise is refused.
        benchmark = snapshot.benchmark_identity(target_path / order["suites"][0]["suite"], source["path"])
        refusals += _benchmark_mismatches(order.get("benchmark"), benchmark)
    elif "benchmark" in order:
        refusals.append(Refusal(f"{adapter.NAME} suites are the subject's own; the order names a benchmark source"))
    if refusals:
        raise Refused(refusals)

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
        system["setup"] = setup  # declared; no placement rules in the prototype, so matched as a whole

    out_dir = Path(out_dir)
    ref = core.order_ref(order)
    slot, round_ = order.get("slot"), order.get("round")
    artifacts_dir = out_dir / "artifacts" / (f"slot-{slot}" if slot is not None else ref[7:19])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    collected = collectors.run_all(order.get("collectors", []), declared=order["target"].get("how_built"),
                                   cwd=target_path, env=child_env, artifacts_dir=artifacts_dir, taken=system)
    if collected.refusals:
        raise Refused(collected.refusals)
    system.update(collected.system)
    order_file = out_dir / f"workorder-{ref[7:]}.json"
    order_file.write_bytes(core.pretty(order))

    attempted = adapter.attempted(applied_protocol)
    try:
        cases = adapter.list_cases(runnable, order["suites"][0].get("filter"), child_env)
    except Unsupported as e:
        raise Refused(e.problems) from None
    workloads = [adapter.workload(case, runnable) if hasattr(adapter, "workload") else (case, {}) for case in cases]
    # Both share workload.parameters: a clash would overwrite the case's own value and merge distinct cases.
    clashes = sorted({name for _, case_parameters in workloads for name in case_parameters} & parameters.keys())
    if clashes:
        raise Refused([Refusal(f"environment variables named like workload parameters: {', '.join(clashes)}; "
                               "rename them")])
    written = []
    for index, case in enumerate(cases):
        stem = f"{index:04d}"
        native_path = artifacts_dir / f"{stem}.native.json"
        load = snapshot.load_average()
        # The wall clock can step backwards mid-case (NTP, WSL2 resyncs): the duration comes from a monotonic clock.
        started, clock = time.time(), time.monotonic()
        run_ = adapter.run_case(runnable, case, invocation, child_env, order["timeouts"]["case_s"], native_path,
                              env_names=env_names)
        duration = time.monotonic() - clock
        (artifacts_dir / f"{stem}.stdout").write_text(run_["stdout"])
        (artifacts_dir / f"{stem}.stderr").write_text(run_["stderr"])
        artifacts = [_artifact("stdout", "text/plain", artifacts_dir / f"{stem}.stdout"),
                     _artifact("stderr", "text/plain", artifacts_dir / f"{stem}.stderr")]
        if native_path.exists():
            artifacts.insert(0, _artifact("native-output", "application/json", native_path))
        artifacts += [_artifact(kind, media_type, path) for kind, media_type, path in run_.get("artifacts", [])]
        artifacts += [_artifact(kind, media_type, path) for kind, media_type, path in collected.artifacts]
        native_system, native_info = adapter.context_facts(run_["native"])
        case_system, case_warnings = snapshot.merge(system, native_system), []
        if hasattr(adapter, "observed_environment"):
            facts, case_warnings = adapter.observed_environment(run_, case, env_names)
            case_system = snapshot.merge(case_system, facts)
        attempt_key = f"urn:benchx:attempt:{ref[7:23]}:{index}"
        workload_name, case_parameters = workloads[index]
        case_parameters = {**case_parameters, **parameters}
        # A harness whose benchmarks declare their own precision reports each case's own protocol.
        case_protocol = (adapter.case_protocol(applied_protocol, run_, case) if hasattr(adapter, "case_protocol")
                         else applied_protocol)
        protocol = {**case_protocol, "harness": adapter.harness(native_info)}

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
            subject = {"name": _subject_name(order), "components": [{"role": "primary"}]}
            if configuration:
                subject["configuration"] = configuration
            key = ingest_key(order["run_key"], workload_name, case_parameters, output["quantity"]["name"], slot)
            document = {
                "schema_version": 5,
                "producer": PROBE_PRODUCER if condition else adapter.PRODUCER,
                "ingest_key": key,
                "attempt_key": attempt_key,
                **({"project": order["project"]} if "project" in order else {}),
                "source": order["target"]["source"],
                "revision": {"key": source["revision"]},
                "benchmark": ({"source": {"uri": benchmark["uri"], "type": benchmark["type"]},
                               **({"revision": {"key": benchmark["revision"]}} if "revision" in benchmark else {})}
                              if benchmark else
                              {"source": order["target"]["source"],
                               # An unversioned tree is identified by its hash in provenance, not by a revision.
                               **({} if source["revision"].startswith("directory-")
                                  else {"revision": {"key": source["revision"]}})}),
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
            warnings = [] if condition else ([setup_warning] if setup_warning else []) + collected.warnings + case_warnings
            if warnings:
                document["quality"] = {"warnings": warnings}
            errors = core.check(document, "measurement-result")
            if errors:
                # harness-adapter.md §4.1: never drop a case, never emit an invalid document.
                document["measurement"] = {"quantity": output["quantity"],
                                           **({"kind": "condition"} if condition else {}),
                                           "status": "error", "reason": "adapter.mapping-failed"}
                document["provenance"]["info"]["mapping_error"] = str(errors[0])
                document["procedure"] = {k: v for k, v in procedure.items()
                                         if k not in ("inner_iterations", "completed_repetitions", "repetition_levels")}
                errors = core.check(document, "measurement-result")
                if errors:
                    raise RuntimeError(f"the error result is itself invalid: {errors[0]}")
            path = out_dir / file_name(key)
            path.write_bytes(core.pretty(document))
            written.append(path)
    return {"order": ref, "cases": len(cases), "results": len(written), "out": str(out_dir),
            "files": [order_file, *written]}
