"""Runner: one #35 work order in, one result per attempt × quantity out.

It changes nothing on the machine (benchmark-environments.md §2). It refuses
an order it cannot carry out exactly, before running anything; every outcome
of a case it did run is a result.
"""

import os
import urllib.parse
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, core, snapshot
from .adapters import gbench
from .core import Refusal, Rejected

RUNNER = {"name": "benchx-prototype", "version": __version__}


class Refused(Rejected):
    """The order cannot be carried out as written; nothing was run."""


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
    if "subject" in order:
        return order["subject"]["name"]
    path = urllib.parse.urlparse(order["source"]["uri"]).path.rstrip("/")
    return path.rsplit("/", 1)[-1].removesuffix(".git") or "subject"


def _artifact(kind, media_type, path: Path) -> dict:
    return {"kind": kind, "media_type": media_type, "uri": path.resolve().as_uri(),
            "sha256": core.sha256_hex(path.read_bytes())}


def check(order: dict) -> Iterator[Refusal]:
    """Everything in a valid order the prototype cannot apply exactly (#35 W5)."""
    if order["harness"]["name"] != "google-benchmark":
        yield Refusal(f"no adapter for harness {order['harness']['name']!r}")
    if order["target"]["kind"] != "build_dir":
        yield Refusal("only build_dir targets are supported; python_env is deferred")
    if order["benchmark"]["kind"] != "subject":
        yield Refusal("only benchmarks in the subject's own checkout are supported")
    if "workload_parameters" in order:
        yield Refusal("google-benchmark takes no workload_parameters")
    unknown = set(order["quantities"]) - set(gbench.QUANTITIES)
    if unknown:
        yield Refusal(f"quantities not supported: {sorted(unknown)}")
    try:
        gbench.protocol(order.get("protocol"))
    except gbench.Unsupported as e:
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
    applied_protocol, flags = gbench.protocol(order.get("protocol"))

    build_dir = Path(order["target"]["build_dir"])
    binary = build_dir / order["suite"]
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        refusals.append(Refusal(f"suite binary not found: {binary}"))
    source_dir = order["target"].get("source_dir") or snapshot.cmake_source_dir(build_dir)
    source = snapshot.git_identity(source_dir) if source_dir else None
    if source is None:
        # runner.md §2: a result that cannot state its revision is a defect.
        refusals.append(Refusal("cannot locate the checkout the target was built from; set target.source_dir"))
    if refusals:
        raise Refused(refusals)

    child_env = {**os.environ, **order.get("environment_variables", {})}
    parameters = dict(order.get("environment_variables", {}))
    environment = snapshot.environment()
    observed = snapshot.observed_context(child_env)
    configuration = snapshot.cmake_configuration(build_dir)
    setup, setup_warning = snapshot.setup_facts(source["path"], child_env)
    if setup:
        observed["setup"] = setup  # declared; no placement rules in the prototype

    out_dir = Path(out_dir)
    ref = core.order_ref(order)
    slot, round_ = order.get("slot"), order.get("round")
    artifacts_dir = out_dir / "artifacts" / (f"slot-{slot}" if slot is not None else ref[7:19])
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    order_file = out_dir / f"workorder-{ref[7:]}.json"
    order_file.write_bytes(core.canonical(order))

    attempted = applied_protocol["repetitions"]["levels"][0]["n"]
    cases = gbench.list_cases(binary, order.get("filter"), child_env)
    written = []
    for index, case in enumerate(cases):
        stem = f"{index:04d}"
        native_path = artifacts_dir / f"{stem}.native.json"
        run_ = gbench.run_case(binary, case, flags, child_env, order.get("timeout_seconds"), native_path)
        (artifacts_dir / f"{stem}.stdout").write_text(run_["stdout"])
        (artifacts_dir / f"{stem}.stderr").write_text(run_["stderr"])
        artifacts = [_artifact("stdout", "text/plain", artifacts_dir / f"{stem}.stdout"),
                     _artifact("stderr", "text/plain", artifacts_dir / f"{stem}.stderr")]
        if native_path.exists():
            artifacts.insert(0, _artifact("native-output", "application/json", native_path))
        native_observed, native_info = gbench.context_facts(run_["native"])
        attempt_key = f"urn:benchx:attempt:{ref[7:23]}:{index}"

        for output in gbench.translate(case, order["quantities"], run_, attempted):
            procedure = dict(output["procedure"], duration_seconds=round(run_["ended"] - run_["started"], 6))
            if round_ is not None:
                procedure["round"] = round_
            if slot is not None:
                procedure["slot"] = slot
            if "timeout_seconds" in order:
                procedure["timeout_seconds"] = order["timeout_seconds"]
            info = {"workorder_ref": ref, **output["info"]}
            if native_info:
                info["google_benchmark"] = native_info
            for key in ("requested_by", "reason"):
                if key in order["provenance"]:
                    info[key] = order["provenance"][key]
            provenance = {
                "run_key": order["provenance"]["run_key"],
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
            if "labels" in order["provenance"]:
                provenance["labels"] = order["provenance"]["labels"]
            workload = {"name": case}
            if parameters:
                workload["parameters"] = parameters
            subject = {"name": _subject_name(order), "components": [{"role": "primary"}]}
            if configuration:
                subject["configuration"] = configuration
            harness = {"name": "google-benchmark"}
            if native_info.get("library_version"):
                harness["version"] = native_info["library_version"]
            key = ingest_key(order["provenance"]["run_key"], case, parameters, output["quantity"]["name"], slot)
            document = {
                "schema_version": 5,
                "producer": gbench.PRODUCER,
                "ingest_key": key,
                "attempt_key": attempt_key,
                **({"project": order["project"]} if "project" in order else {}),
                "source": order["source"],
                "revision": {"key": source["revision"]},
                "benchmark": {"source": order["source"], "revision": {"key": source["revision"]}},
                "coordinates": {
                    "workload": workload,
                    "subject": subject,
                    "quantity": output["quantity"],
                    "comparison_context": {"harness": harness, "protocol": applied_protocol},
                    "environment": environment,
                },
                "measurement": output["measurement"],
                "observed_context": {**observed, **({"google_benchmark": native_observed} if native_observed else {})},
                "procedure": procedure,
                "provenance": provenance,
            }
            if setup_warning:
                document["quality"] = {"warnings": [setup_warning]}
            errors = core.check(document, "measurement-result")
            if errors:
                # harness-adapter.md §4.1: never drop a case, never emit an invalid document.
                document["measurement"] = {"status": "error", "reason": "adapter.mapping-failed"}
                document["provenance"]["info"]["mapping_error"] = str(errors[0])
                document["procedure"] = {k: v for k, v in procedure.items()
                                         if k not in ("inner_iterations", "completed_repetitions")}
                errors = core.check(document, "measurement-result")
                if errors:
                    raise RuntimeError(f"the error result is itself invalid: {errors[0]}")
            path = out_dir / file_name(key)
            path.write_bytes(core.canonical(document))
            written.append(path)
    return {"order": ref, "cases": len(cases), "results": len(written), "out": str(out_dir),
            "files": [order_file, *written]}
