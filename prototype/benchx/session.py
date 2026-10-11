"""Session loop: run two prepared targets side by side, then compare them.

This is the workbench's part (system-decomposition.md §3.4; prototype-design.md
§5, measure mode). The runner takes one order per invocation and no harness can
interleave two separate binaries, so something above the runner has to write one
work order per side and round, run them alternately, and hand the run to the
comparator. That loop lives here, and the CLI, a CI script, or a front end such
as `spin bench --compare` calls it.

The session builds nothing itself. Both targets must exist when it looks; a
target provider it calls may build them first (`prepare`), and plays the
target-provider role.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import compare as compare_mod
from . import runner, snapshot, target as target_mod

PROFILES = ("revisions", "environments")
CASE_TIMEOUT_S, ORDER_TIMEOUT_S = 60, 600


class SessionError(Exception):
    """The session cannot start or finish; the message says why."""


def parse_target(text: str) -> tuple[str, str | None]:
    """`BUILD_DIR[:SOURCE_DIR]`."""
    build, _, source = text.partition(":")
    if not build:
        raise SessionError(f"empty build directory in target {text!r}")
    return build, source or None


def parse_label(text: str) -> tuple[str, str, str]:
    """`NAME=BASE,CONTENDER`, the two label values that name the sides."""
    name, _, values = text.partition("=")
    base, comma, contender = values.partition(",")
    if not (name and base and comma and contender) or "," in contender:
        raise SessionError(f"--label must be NAME=BASE,CONTENDER in measure mode, not {text!r}")
    return name, base, contender


def _source_uri(source_dir: str) -> str:
    """One canonical URI for every worktree of a repository, so both sides of a
    comparison report the same source."""
    origin = snapshot._run(["git", "remote", "get-url", "origin"], cwd=source_dir)
    if origin:
        return origin
    common = snapshot._run(["git", "rev-parse", "--git-common-dir"], cwd=source_dir)
    return Path(source_dir, common).resolve().parent.as_uri() if common else Path(source_dir).resolve().as_uri()


def _side(spec: str, suite: str, operation: str) -> dict:
    """Locate and identify one side before anything runs.

    `spec` is `@CONFIG[=SOURCE_REF]` (ask the provider to `operation`) or
    `BUILD_DIR[:SOURCE_DIR]` (read the build directory's sidecar, if any).
    """
    try:
        description = target_mod.resolve(spec, operation=operation)
    except target_mod.TargetError as e:
        raise SessionError(str(e)) from None
    source = None
    if description:
        if description["target"]["kind"] != "build":
            raise SessionError(f"{spec}: only build targets are supported; "
                               f"{description['target']['kind']!r} is deferred")
        if "build" in description:
            raise SessionError(f"{spec}: a declared build is deferred in the prototype "
                               "(the runner reads CMakeCache.txt)")
        build = description["target"]["path"]
        source = description["target"].get("source_dir")
        uri = description["target"]["source"]["uri"]
    else:
        build, source = parse_target(spec)
        uri = None
    build = Path(build).resolve()
    if not build.is_dir():
        raise SessionError(f"build directory not found: {build}")
    if not (build / suite).is_file():
        raise SessionError(f"suite binary not found: {build / suite}")
    if ":" in spec and not spec.startswith("@"):
        source = parse_target(spec)[1] or source
    source = source or snapshot.cmake_source_dir(build)
    identity = source and snapshot.git_identity(source)
    if not identity:
        raise SessionError(f"cannot locate the checkout {build} was built from; give BUILD_DIR:SOURCE_DIR")
    return {"build": build, "source": identity["path"], "identity": identity, "uri": uri,
            "text": target_mod.text(description), "labels": dict((description or {}).get("labels", {}))}


def _order(side: dict, *, suite, filter, quantity, repetitions, min_time, project, run_key, source_uri,
           round_, slot, labels) -> dict:
    precision = {"repetitions": repetitions}
    if min_time is not None:
        precision["calibration"] = {"mode": "adaptive", "minimum_sample_seconds": min_time}
    entry = {"adapter": "google-benchmark", "suite": suite}
    if filter:
        entry["filter"] = filter
    order = {
        "schema_version": "benchx/work-order/0.1.0",
        "work_order_id": f"urn:uuid:{uuid.uuid4()}",
        "state": "resolved",
        "suites": [entry],
        "target": {"kind": "build", "path": str(side["build"]), "source_dir": side["source"],
                   "source": {"uri": source_uri, "type": "git"}, **side["text"]},
        "quantities": [quantity],
        "precision": precision,
        "timeouts": {"case_s": CASE_TIMEOUT_S, "order_s": ORDER_TIMEOUT_S},
        "run_key": run_key,
        "requester": {"kind": "workbench", "name": "bx"},
        # Placeholder: the runner does not yet cross-check plan against the
        # case list it discovers from the binary (runner-schema.md §3.2).
        "plan": [{"id": "p0", "case": "*", "quantity": quantity}],
        "round": round_,
        "slot": slot,
    }
    labels = {**side["labels"], **(labels or {})}
    if labels:
        order["labels"] = labels
    if project:
        order["project"] = project
    return order


def compare_targets(baseline: str, contender: str, *, profile: str, suite: str, rounds: int, store,
                    filter=None, quantity="wall-time", repetitions=5, min_time=None, project=None,
                    label=None, run_key=None, out=None, source_uri=None, k=3.0, min_rounds=3,
                    progress=None, build=True) -> dict:
    """Alternate baseline and contender over `rounds` rounds, deliver every
    result to `store`, and return the run-mode comparison document.

    `baseline` and `contender` are `BUILD_DIR[:SOURCE_DIR]` or `@CONFIG[=SOURCE_REF]`;
    a provider is asked to `prepare` (it may build) unless `build` is false, when
    it is only asked to `describe`. For `environments`,
    `label` is (name, baseline value, contender value). Raises SessionError when
    the session cannot start, and runner.Refused when the runner refuses an order.
    """
    if profile not in PROFILES:
        raise SessionError(f"profile {profile!r} is not in the prototype ({', '.join(PROFILES)})")
    if rounds < 1:
        raise SessionError("--rounds must be at least 1")
    if profile == "environments" and label is None:
        raise SessionError("the environments profile needs --label NAME=BASE,CONTENDER")
    if profile == "revisions" and label is not None:
        raise SessionError("--label names the sides of an environments comparison; revisions are named by their trees")
    operation = "prepare" if build else "describe"
    sides = [_side(baseline, suite, operation), _side(contender, suite, operation)]
    if profile == "revisions":
        # UC-03 §10: refuse before any measurement, not after R rounds.
        for name, side in zip(("baseline", "contender"), sides):
            if side["identity"]["dirty"] != "clean":
                raise SessionError(f"{name} tree {side['source']} is {side['identity']['dirty']}; "
                                   "a revisions comparison needs clean trees (UC-03 §10)")
    uris = {source_uri or side["uri"] or _source_uri(side["source"]) for side in sides}
    if len(uris) != 1:
        raise SessionError(f"the sides report different source URIs {sorted(uris)}; pass --source-uri")
    source_uri = uris.pop()

    run_key = run_key or "compare-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(out) if out else Path("results") / run_key.replace("/", "_")
    (out / "orders").mkdir(parents=True, exist_ok=True)
    labels = [{label[0]: label[1]}, {label[0]: label[2]}] if label else [None, None]

    slot = 0
    for round_ in range(rounds):
        for name, side, side_labels in zip(("baseline", "contender"), sides, labels):
            order = _order(side, suite=suite, filter=filter, quantity=quantity, repetitions=repetitions,
                           min_time=min_time, project=project, run_key=run_key, source_uri=source_uri,
                           round_=round_, slot=slot, labels=side_labels)
            path = out / "orders" / f"order-{slot:04d}.json"
            path.write_text(json.dumps(order, indent=1))
            summary = runner.run(path, out)
            outcome = store.ingest_paths(summary["files"])
            if outcome["rejected"]:
                codes = sorted({r["code"] for r in outcome["rejected"]})
                raise SessionError(f"the store rejected results of round {round_} ({name}): {', '.join(codes)}")
            if progress:
                progress(f"round {round_ + 1}/{rounds} {name}: {summary['results']} result(s)")
            slot += 1

    base_value = label[1] if profile == "environments" else sides[0]["identity"]["tree"]
    return compare_mod.compare(store.documents(run_key), run_key=run_key, profile=profile,
                               baseline=base_value, label=label[0] if label else None, k=k,
                               min_rounds=min_rounds)
