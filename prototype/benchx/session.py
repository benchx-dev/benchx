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
import os
from pathlib import Path

from . import adapters, harnesses, orders, runner, snapshot, target as target_mod
from . import compare as compare_mod
from .adapters.base import Unsupported
from .errors import SessionError
from .identity import normalize_remote

PROFILES = ("revisions", "environments")


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


KINDS = ("build", "working_tree")


def _side(spec: str, suite: str, operation: str, harness, environment_variables=None, provider=None) -> dict:
    """Locate and identify one side before anything runs.

    `spec` is `@CONFIG[=SOURCE_REF]` (ask the provider to `operation`) or a
    bare path, which the harness reads: Google Benchmark's is
    `BUILD_DIR[:SOURCE_DIR]` (read the build directory's sidecar, if any), and
    pyperf's is a working tree, its own source. A provider's description says
    which kind it is. The harness's adapter checks that the suite is there and
    usable, as the runner will, so a side it would refuse is refused before
    any round runs.
    """
    try:
        description = target_mod.resolve(spec, operation=operation, provider=provider)
    except target_mod.TargetError as e:
        raise SessionError(str(e)) from None
    source = None
    if description:
        kind = description["target"]["kind"]
        if kind not in KINDS:
            raise SessionError(f"{spec}: only build and working_tree targets are supported; {kind!r} is deferred")
        if "build" in description:
            raise SessionError(f"{spec}: a declared build is deferred in the prototype "
                               "(the runner reads CMakeCache.txt)")
        path = description["target"]["path"]
        # A working tree is its own source, as for the runner.
        source = description["target"].get("source_dir") or (path if kind == "working_tree" else None)
        uri = description["target"]["source"]["uri"]
    else:
        kind = harness.BARE_PATH_KIND
        path, source = parse_target(spec) if kind == "build" else (spec, spec)  # a working tree is its own source
        uri = None
    path = Path(path).resolve()
    if not path.is_dir():
        raise SessionError(f"{'build directory' if kind == 'build' else 'working tree'} not found: {path}")
    try:
        # The environment the runner will give the harness: inherited, plus the order's variables.
        adapters.get(harness.name).locate(path, suite, {**os.environ, **(environment_variables or {})})
    except Unsupported as e:
        raise SessionError("; ".join(str(problem) for problem in e.problems)) from None
    if kind == "build" and ":" in spec and not spec.startswith("@"):
        source = parse_target(spec)[1] or source
    if kind == "build":
        source = source or snapshot.cmake_source_dir(path)
        identity = source and snapshot.git_identity(source)
        if not identity:
            raise SessionError(f"cannot locate the checkout {path} was built from; give BUILD_DIR:SOURCE_DIR")
    else:
        identity = snapshot.git_identity(source) or snapshot.directory_identity(source)
        if not identity:
            raise SessionError(f"cannot identify the working tree {source}")
    found = _found_benchmark(harness, path, suite, identity["path"])
    return {"kind": kind, "path": path, "source": identity["path"], "identity": identity, "uri": uri,
            "benchmark": _benchmark(found),
            # As the runner records it: a benchmark in the subject's tree is dirty as the subject is.
            "benchmark_dirty": found["dirty"] if found else identity["dirty"],
            "text": target_mod.text(description), "labels": dict((description or {}).get("labels", {}))}


def _found_benchmark(harness, path: Path, suite: str, subject_source: str) -> dict | None:
    """The identity of the suite's code when it is not in the subject's checkout (see
    snapshot.benchmark_identity), else None. A suite that is a built binary is the subject's own."""
    if not adapters.get(harness.name).SUITE_IS_SOURCE:
        return None
    return snapshot.benchmark_identity(path / suite, subject_source)


def _benchmark(found: dict | None) -> dict | None:
    """The order's `benchmark` entry for a suite found outside the subject's checkout."""
    if found is None:
        return None
    return {"source": {"uri": found["uri"], "type": found["type"]},
            **({"revision": found["revision"]} if "revision" in found else {})}


def _order(side: dict, *, harness, suite, filter, quantities, precision, environment_variables, project, run_key,
           source_uri, round_=None, slot=None, labels=None, case_s=orders.CASE_TIMEOUT_S,
           order_s=orders.ORDER_TIMEOUT_S, collectors=None) -> dict:
    """One side's order; for one round and slot of a comparison, or neither (`bx bench`)."""
    target = {"kind": side["kind"], "path": str(side["path"]), "source_dir": side["source"],
              "source": {"uri": source_uri, "type": "git"}, **side["text"]}
    try:
        return orders.make_order(suite=harness.suite_entry(suite, filter), target=target, precision=precision,
                                 quantities=quantities, run_key=run_key, project=project,
                                 labels={**side["labels"], **(labels or {})}, case_s=case_s, order_s=order_s,
                                 environment_variables=environment_variables, round_=round_, slot=slot,
                                 benchmark=side.get("benchmark"), collectors=collectors)
    except orders.OrderError as e:
        raise SessionError(f"work order{'' if round_ is None else f' for round {round_}'}: {e}") from None


def _harness(adapter, flags, file_precision=None, quantities=None) -> tuple:
    """The harness named by `adapter`, and its complete precision from `flags` over
    the config file's precision over its defaults. A flag the harness does not
    take, a precision or a quantity it cannot apply, is refused here, before any
    side is resolved."""
    if not adapter:
        raise SessionError("no adapter given: pass --adapter, or set adapter.name in benchx.conf.json")
    harness = harnesses.get(adapter)
    precision = harness.precision(harness.flags_to_precision(**(flags or {})), file_precision)
    try:
        adapters.get(harness.name).protocol(precision)
    except Unsupported as e:
        raise SessionError("; ".join(str(problem) for problem in e.problems)) from None
    unknown = sorted(set(quantities or ()) - set(adapters.get(harness.name).QUANTITIES))
    if unknown:
        raise SessionError(f"{harness.name} does not report {', '.join(unknown)}")
    return harness, precision


def _source_uri(sides, source_uri) -> str:
    """The one URI the sides' source is known by; SessionError if they disagree."""
    if source_uri:
        uris = {normalize_remote(source_uri)}
    else:
        uris = {normalize_remote(side["uri"], base=side["source"]) if side["uri"] else snapshot.source_uri(side["source"])
                for side in sides}
    if len(uris) != 1:
        raise SessionError(f"the sides report different source URIs {sorted(uris)}; pass --source-uri")
    return uris.pop()


def _run_side(side: dict, path: Path, store=None, **order) -> tuple[dict, dict | None]:
    """Write one side's order to `path`, run it (results go beside `orders/`),
    and deliver them to `store` unless it is None. The runner's summary, and the
    store's outcome (None without a store)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_order(side, **order), indent=1) + "\n")
    # The order is this session's own, built from the user's configuration: its collectors may run.
    summary = runner.run(path, path.parent.parent, allow_collectors=True)
    return summary, store.ingest_paths(summary["files"]) if store else None


def bench_target(target: str, *, suite: str, adapter: str, store=None, flags=None, file_precision=None,
                 environment_variables=None, filter=None, quantities=("wall-time",), project=None, run_key=None,
                 out=None, source_uri=None, case_s=orders.CASE_TIMEOUT_S, order_s=orders.ORDER_TIMEOUT_S,
                 progress=None, build=True, provider=None, config_path=None, results_dir=None,
                 collectors=None, round_=None, slot=None, labels=None) -> dict:
    """Run one target once: a comparison session of one order, with no comparison.
    It has a round, a slot and labels when the caller places it in a session that
    other calls join through the same run key. Results are delivered to `store`
    unless it is None.

    `target` is `@CONFIG[=SOURCE_REF]` or a bare path, which the harness reads
    (see `_side`); `adapter`, `flags` and the rest are as for `compare_targets`.
    Returns the runner's summary, with the store's `outcome` and the `precision`.
    Raises SessionError when the run cannot start, and runner.Refused when the
    runner refuses the order.
    """
    harness, precision = _harness(adapter, flags, file_precision, quantities)
    side = _side(target, suite, "prepare" if build else "describe", harness, environment_variables, provider)
    source_uri = _source_uri([side], source_uri)
    run_key = run_key or orders.default_run_key("bench")
    out = Path(out) if out else Path(results_dir or "results") / run_key.replace("/", "_")
    if progress:
        progress(f"precision: {harness.describe_precision(precision, config_path)}")
    order_file = "order.json" if slot is None else f"order-{slot:04d}.json"  # one per slot, as compare_targets
    summary, outcome = _run_side(side, out / "orders" / order_file, store, harness=harness, suite=suite,
                                 filter=filter, quantities=quantities, precision=precision,
                                 environment_variables=environment_variables, project=project, run_key=run_key,
                                 source_uri=source_uri, case_s=case_s, order_s=order_s, collectors=collectors,
                                 round_=round_, slot=slot, labels=labels)
    return {**summary, "outcome": outcome, "precision": precision}


def compare_targets(baseline: str, contender: str, *, profile: str, suite: str, rounds: int, store,
                    adapter, flags=None, file_precision=None, environment_variables=None, filter=None,
                    quantities=("wall-time",), project=None, label=None, run_key=None, out=None, source_uri=None,
                    k=3.0, min_rounds=3, progress=None, build=True, provider=None, results_dir=None,
                    case_s=orders.CASE_TIMEOUT_S, order_s=orders.ORDER_TIMEOUT_S, config_path=None,
                    collectors=None) -> dict:
    """Alternate baseline and contender over `rounds` rounds, deliver every
    result to `store`, and return the run-mode comparison document.

    `baseline` and `contender` are `BUILD_DIR[:SOURCE_DIR]` or `@CONFIG[=SOURCE_REF]`;
    a provider is asked to `prepare` (it may build) unless `build` is false, when
    it is only asked to `describe`. For `environments`,
    `label` is (name, baseline value, contender value).

    `adapter` names the harness; `flags` are its precision flags by keyword
    (`harnesses.Harness.FLAGS`), over `file_precision` (the config file's) and
    its defaults. `provider` is the (command, directory) a `@CONFIG` target
    calls. `quantities` are collected in every order. `environment_variables`
    are set for both sides alike, and so become workload parameters of both.
    Raises SessionError when the session cannot start, and runner.Refused when
    the runner refuses an order.
    """
    if profile not in PROFILES:
        raise SessionError(f"profile {profile!r} is not in the prototype ({', '.join(PROFILES)})")
    if rounds < 1:
        raise SessionError("--rounds must be at least 1")
    if profile == "environments" and label is None:
        raise SessionError("the environments profile needs --label NAME=BASE,CONTENDER")
    if profile == "revisions" and label is not None:
        raise SessionError("--label names the sides of an environments comparison; revisions are named by their trees")
    harness, precision = _harness(adapter, flags, file_precision, quantities)
    operation = "prepare" if build else "describe"
    sides = [_side(spec, suite, operation, harness, environment_variables, provider) for spec in (baseline, contender)]
    if profile == "revisions":
        # UC-03 §10: a dirty side is compared and labeled local-only. A side with no working-tree id
        # (its dirty state is `unknown`) cannot be named, so it is refused before any measurement,
        # not after R rounds.
        for name, side in zip(("baseline", "contender"), sides):
            if side["identity"]["dirty"] == "unknown":  # schema §4.1: no tree id exactly when unknown
                raise SessionError(f"{name} tree {side['source']} has no working-tree id (its dirty state is "
                                   f"{side['identity']['dirty']}): the sides cannot be told apart, or checked for "
                                   "a change during the run. Make every file in the tree readable, or git-ignore "
                                   "it (UC-03 §10)")
    if progress:  # TODO: log these at logging.WARNING once benchx has logging (plan, Q13)
        for name, side in zip(("baseline", "contender"), sides):
            for what, state in (("tree", side["identity"]["dirty"]),
                                ("benchmark file", side["benchmark_dirty"] if side["benchmark"] else "clean")):
                if state != "clean":
                    where = f" {side['source']}" if what == "tree" else ""
                    progress(f"{name} {what}{where} is {state}: the comparison will be local-only")
    source_uri = _source_uri(sides, source_uri)
    if len({json.dumps((side.get("benchmark") or {}).get("source")) for side in sides}) != 1:
        raise SessionError("the sides' suites come from different sources; give --suite as one absolute path")

    run_key = run_key or orders.default_run_key("compare")
    out = Path(out) if out else Path(results_dir or "results") / run_key.replace("/", "_")
    labels = [{label[0]: label[1]}, {label[0]: label[2]}] if label else [None, None]

    slot = 0
    for round_ in range(rounds):
        for name, side, side_labels in zip(("baseline", "contender"), sides, labels):
            summary, outcome = _run_side(side, out / "orders" / f"order-{slot:04d}.json", store,
                                         harness=harness, suite=suite, filter=filter, quantities=quantities,
                                         precision=precision, environment_variables=environment_variables,
                                         project=project, run_key=run_key, source_uri=source_uri,
                                         round_=round_, slot=slot, labels=side_labels, case_s=case_s, order_s=order_s,
                                         collectors=collectors)
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
