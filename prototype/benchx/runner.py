"""Runner: one work order in, one result per attempt × quantity out.

It changes nothing on the machine (benchmark-environments.md §2). It refuses
an order it cannot carry out exactly, before running anything; every outcome
of a case it did run is a result.
"""

import os
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, adapters, core, snapshot
from . import target as target_mod
from .adapters.base import Unsupported
from .core import Refusal, Rejected

RUNNER = {"name": "benchx-prototype", "version": __version__}


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
    if "benchmark" in order:
        yield Refusal("only benchmarks in the subject's own checkout are supported")
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


def run(order_path, out_dir) -> dict:
    """Execute one order; write results and the order into out_dir."""
    inspection = core.inspect(Path(order_path), "work-order")
    if inspection.data is None:
        raise Refused(inspection.errors)
    order = inspection.data
    refusals = list(check(order))
    if refusals:
        raise Refused(refusals)
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
    if refusals:
        raise Refused(refusals)

    parameters = dict(order.get("environment_variables", {}))
    environment = snapshot.environment()
    env_names = snapshot.env_allowlist(child_env, getattr(adapter, "ENV_ALLOWLIST", ()))
    observed = snapshot.observed_context(child_env, env_names)
    # Declared by the user, recorded as given, never executed or checked (runner-schema.md §3.1).
    declared_text = target_mod.text(order["target"])
    if "provider" in declared_text:
        declared_text["target_provider"] = declared_text.pop("provider")
    setup, setup_warning = snapshot.setup_facts(source["path"], child_env)
    if setup:
        observed["setup"] = setup  # declared; no placement rules in the prototype

    out_dir = Path(out_dir)
    ref = core.order_ref(order)
    slot, round_ = order.get("slot"), order.get("round")
    artifacts_dir = out_dir / "artifacts" / (f"slot-{slot}" if slot is not None else ref[7:19])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    order_file = out_dir / f"workorder-{ref[7:]}.json"
    order_file.write_bytes(core.pretty(order))

    attempted = adapter.attempted(applied_protocol)
    try:
        cases = adapter.list_cases(runnable, order["suites"][0].get("filter"), child_env)
    except Unsupported as e:
        raise Refused(e.problems) from None
    written = []
    for index, case in enumerate(cases):
        stem = f"{index:04d}"
        native_path = artifacts_dir / f"{stem}.native.json"
        run_ = adapter.run_case(runnable, case, invocation, child_env, order["timeouts"]["case_s"], native_path,
                              env_names=env_names)
        (artifacts_dir / f"{stem}.stdout").write_text(run_["stdout"])
        (artifacts_dir / f"{stem}.stderr").write_text(run_["stderr"])
        artifacts = [_artifact("stdout", "text/plain", artifacts_dir / f"{stem}.stdout"),
                     _artifact("stderr", "text/plain", artifacts_dir / f"{stem}.stderr")]
        if native_path.exists():
            artifacts.insert(0, _artifact("native-output", "application/json", native_path))
        artifacts += [_artifact(kind, media_type, path) for kind, media_type, path in run_.get("artifacts", [])]
        native_observed, native_info = adapter.context_facts(run_["native"])
        case_observed, case_warnings = observed, []
        if hasattr(adapter, "observed_environment"):
            facts, case_warnings = adapter.observed_environment(run_, case, env_names)
            case_observed = {k: v for k, v in {**observed, **facts}.items() if v is not None}
        attempt_key = f"urn:benchx:attempt:{ref[7:23]}:{index}"

        for output in adapter.translate(case, order["quantities"], run_, attempted):
            procedure = dict(output["procedure"], duration_seconds=round(run_["ended"] - run_["started"], 6))
            if round_ is not None:
                procedure["round"] = round_
            if slot is not None:
                procedure["slot"] = slot
            procedure["timeout_seconds"] = order["timeouts"]["case_s"]
            if hasattr(adapter, "repetition_levels"):
                procedure["repetition_levels"] = adapter.repetition_levels(applied_protocol, run_, case)
            info = {"workorder_ref": ref, "requester": order["requester"], **output["info"]}
            if native_info:
                info[adapter.CONTEXT_KEY] = native_info
            if "reason" in order:
                info["reason"] = order["reason"]
            info.update(declared_text)
            provenance = {
                "run_key": order["run_key"],
                "started_at": _timestamp(run_["started"]),
                "ended_at": _timestamp(run_["ended"]),
                "subject_dirty": source["dirty"],
                "benchmark_dirty": source["dirty"],
                "runner": RUNNER,
                "artifacts": artifacts,
                "info": info,
            }
            if "tree" in source:
                provenance["subject_tree"] = provenance["benchmark_tree"] = source["tree"]
            if "labels" in order:
                provenance["labels"] = order["labels"]
            workload = {"name": case}
            if parameters:
                workload["parameters"] = parameters
            subject = {"name": _subject_name(order), "components": [{"role": "primary"}]}
            if configuration:
                subject["configuration"] = configuration
            harness = adapter.harness(native_info)
            key = ingest_key(order["run_key"], case, parameters, output["quantity"]["name"], slot)
            document = {
                "schema_version": 5,
                "producer": adapter.PRODUCER,
                "ingest_key": key,
                "attempt_key": attempt_key,
                **({"project": order["project"]} if "project" in order else {}),
                "source": order["target"]["source"],
                "revision": {"key": source["revision"]},
                "benchmark": {"source": order["target"]["source"],
                              # An unversioned tree is identified by its hash in provenance, not by a revision.
                              **({} if source["revision"].startswith("directory-")
                                 else {"revision": {"key": source["revision"]}})},
                "coordinates": {
                    "workload": workload,
                    "subject": subject,
                    "quantity": output["quantity"],
                    "comparison_context": {"harness": harness, "protocol": applied_protocol,
                                           **(adapter.comparison_extras(native_info)
                                              if hasattr(adapter, "comparison_extras") else {})},
                    "environment": environment,
                },
                "measurement": output["measurement"],
                "observed_context": {**case_observed, **({adapter.CONTEXT_KEY: native_observed} if native_observed else {})},
                "procedure": procedure,
                "provenance": provenance,
            }
            warnings = ([setup_warning] if setup_warning else []) + case_warnings
            if warnings:
                document["quality"] = {"warnings": warnings}
            errors = core.check(document, "measurement-result")
            if errors:
                # harness-adapter.md §4.1: never drop a case, never emit an invalid document.
                document["measurement"] = {"status": "error", "reason": "adapter.mapping-failed"}
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
