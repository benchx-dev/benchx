"""bx: a thin command line over the benchx package (prototype-design.md §1).

The local store is always ~/.benchx/store.parquet ($BENCHX_HOME relocates
benchx's home). `bx run` writes result files and ingests them there.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import get_args

from . import bench as bench_mod
from . import compare as compare_mod
from . import config as config_mod
from . import core, runner, session
from . import target as target_mod
from .core.validation import Kind
from .store import Store

EXIT_OK = 0
EXIT_INVALID = 1  # a document invalid, a result rejected, or a target without a description
EXIT_REFUSED = 2  # the command was refused or could not be carried out


def _refused(error: runner.Refused) -> int:
    for problem in error.problems:
        print(f"refused: {problem}", file=sys.stderr)
    return EXIT_REFUSED


def cmd_run(args):
    """Run one order, write its result files, and deliver them to the local
    store. The runner itself stays store-unaware (runner.md §2 principle 6);
    this command composes it with ingest, as the workbench does."""
    out = Path(args.out) if args.out else Path("results")
    try:
        summary = runner.run(args.order, out)
    except runner.Refused as e:
        return _refused(e)
    print(f"{summary['results']} result(s) for {summary['cases']} case(s) -> {summary['out']}"
          f"  (order {summary['order'][:19]})")
    if args.no_ingest:
        return EXIT_OK
    store = Store()
    print(f"{store.path}: ", end="")
    return _report(store.ingest_paths(summary["files"]))


def cmd_bench(args):
    """Benchmark functions of a Python module with pyperf: build the order from
    the config file, flags and environment variables, run it, deliver, print a table."""
    env = {}
    for item in args.env or []:
        name, eq, value = item.partition("=")
        if not (name and eq):
            print(f"bx bench: error: -e wants NAME=VALUE, not {item!r}", file=sys.stderr)
            return EXIT_REFUSED
        env[name] = value
    try:
        flags = bench_mod.flags_to_precision(args.processes, args.values, args.min_time, args.loops, args.warmups)
        order, config_path = bench_mod.build_order(
            args.module, args.functions, tree=args.tree, precision_flags=flags, environment_variables=env,
            project=args.project, run_key=args.run_key, case_s=args.case_timeout)
        out = Path(args.out) if args.out else Path("results") / order["run_key"]
        reps = {lv["unit"]: lv["n"] for lv in order["precision"]["repetitions"]["levels"]}
        print(f"precision: {reps['process']} processes x {reps['value']} values "
              f"(config: {config_path or 'none, pyperf defaults'})", file=sys.stderr)
        summary = bench_mod.run_order(order, out)
    except (bench_mod.SessionError, config_mod.ConfigError) as e:
        print(f"bx bench: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except runner.Refused as e:
        return _refused(e)
    print(bench_mod.render(bench_mod.summarize(summary["files"])))
    print(f"\n{summary['results']} result(s) -> {summary['out']}  (order {summary['order'][:19]})")
    if args.no_ingest:
        return EXIT_OK
    store = Store()
    print(f"{store.path}: ", end="")
    return _report(store.ingest_paths(summary["files"]))


def cmd_config(args):
    try:
        path = config_mod.write_template(args.dir)
    except config_mod.ConfigError as e:
        print(f"bx config: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"wrote {path}")
    return EXIT_OK


def _report(outcome):
    for r in outcome["rejected"]:
        print(f"  rejected {r['document']}: {r['code']} — {r['message']}", file=sys.stderr)
    counts = f"ingested {outcome['ingested']}, duplicate {outcome['duplicate']}"
    if outcome["rejected"]:
        codes = sorted({r["code"] for r in outcome["rejected"]})
        counts += f", rejected {len(outcome['rejected'])} ({', '.join(codes)})"
    print(counts)
    return EXIT_INVALID if outcome["rejected"] else EXIT_OK


def cmd_ingest(args):
    return _report(Store().ingest_paths(args.paths))


def cmd_series(args):
    for s in Store().series(args.workload, args.quantity):
        estimator = json.loads(s["estimator"])["method_version"]
        print(f"{s['fingerprint'][:12]}  {s['project']}  {s['workload']}  {s['quantity']} [{s['unit']}]"
              f"  est={estimator}")
    return EXIT_OK


def cmd_history(args):
    points = Store().history(args.series)
    print("revision order unavailable without a revision graph; listed by measurement start")
    for p in points:
        print(f"{p['revision'][:12]}  {p['started_at']}  {p['value']:.6g} ({p['source']})  {p['ingest_key']}")
    return EXIT_OK


def cmd_head(args):
    store = Store()
    total, rows = store.head(args.n)
    if args.json:
        for row in rows:
            print(row["document"])
        return EXIT_OK
    print(f"{store.path}: {total} result(s); latest {len(rows)}, newest first")
    for r in rows:
        median = f"{r['median']:.6g} {r['unit']}" if r["median"] is not None else "-"
        started = r["started_at"].strftime("%Y-%m-%d %H:%M:%S")
        labels = f"  {r['labels']}" if r["labels"] else ""
        print(f"{r['row']:>5}  {started}  {r['run_key']:<24} {r['workload']:<16} {r['quantity']:<9} "
              f"{r['status']:<8} {median:<14} {r['revision'][:10]}  {r['project']}{labels}")
    return EXIT_OK


def cmd_target(args):
    """Ask for a target's description: `describe` only reports, `prepare` may
    build (the provider's own tools do that, never benchx)."""
    try:
        description = target_mod.resolve(args.target, operation=args.operation)
    except target_mod.TargetError as e:
        print(f"cannot {args.operation} {args.target}: {e}", file=sys.stderr)
        return EXIT_REFUSED
    if description is None:
        print(f"{args.target} has no description: no {target_mod.SIDECAR} in it", file=sys.stderr)
        return EXIT_INVALID
    print(json.dumps(description, indent=1))
    return EXIT_OK


class UsageError(Exception):
    """The arguments do not select a mode, or mix the two."""


# Flags that belong to one mode of `bx compare` (prototype-design.md §1).
_MEASURE_ONLY = ("suite", "filter", "quantity", "rounds", "repetitions", "min_time", "project", "run_key",
                 "out", "source_uri", "no_build")
_READ_ONLY = ("baseline", "results")


def _flags(args, names):
    return [f"--{n.replace('_', '-')}" for n in names if getattr(args, n) not in (None, [], False)]


def _mode(args) -> str:
    if args.targets and args.run:
        raise UsageError("give two targets or --run, not both")
    if not args.targets and not args.run:
        raise UsageError("give two targets (measure mode) or --run KEY (read mode)")
    if args.targets:
        if len(args.targets) != 2:
            raise UsageError("measure mode takes exactly two targets: BASELINE CONTENDER")
        if wrong := _flags(args, _READ_ONLY):
            raise UsageError(f"{', '.join(wrong)} belongs to read mode (--run)")
        for need in ("suite", "rounds"):
            if getattr(args, need) is None:
                raise UsageError(f"measure mode needs --{need}")
        return "measure"
    if wrong := _flags(args, _MEASURE_ONLY):
        raise UsageError(f"{', '.join(wrong)} belongs to measure mode (two targets)")
    if not args.baseline:
        raise UsageError("read mode needs --baseline")
    return "read"


def cmd_compare(args):
    try:
        mode = _mode(args)
    except UsageError as e:
        print(f"bx compare: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    return _measure(args) if mode == "measure" else _read(args)


def _read(args):
    if args.results:
        with tempfile.TemporaryDirectory() as tmp:  # the ad hoc path: a throwaway store
            store = Store(Path(tmp) / "store.parquet")
            outcome = store.ingest_paths(args.results)
            if outcome["rejected"]:
                return _report(outcome)
            return _compare(store, args)
    return _compare(Store(), args)


def _measure(args):
    """The workbench's session loop: run both sides, deliver, compare."""
    store = Store()
    print(f"{store.path}", file=sys.stderr)  # stderr, so --json output stays clean
    try:
        label = session.parse_label(args.label) if args.label else None
        doc = session.compare_targets(
            *args.targets, profile=args.profile, suite=args.suite, rounds=args.rounds, store=store,
            filter=args.filter, quantity=args.quantity or "wall-time", repetitions=args.repetitions or 5,
            min_time=args.min_time, project=args.project, label=label, run_key=args.run_key, out=args.out,
            source_uri=args.source_uri, k=args.k, min_rounds=args.min_rounds, build=not args.no_build,
            progress=lambda message: print(message, file=sys.stderr))
    except runner.Refused as e:
        return _refused(e)
    except (session.SessionError, compare_mod.CompareError) as e:
        print(f"cannot compare: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(doc, indent=1) if args.json else compare_mod.render(doc))
    return EXIT_OK


def _compare(store, args):
    try:
        doc = compare_mod.compare(store.documents(args.run), run_key=args.run, profile=args.profile,
                                  baseline=args.baseline, label=args.label, k=args.k,
                                  min_rounds=args.min_rounds)
    except compare_mod.CompareError as e:
        print(f"cannot compare: {e}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(doc, indent=1) if args.json else compare_mod.render(doc))
    return EXIT_OK


def cmd_validate(args):
    valid = 0
    for path in args.files:
        inspection = core.inspect(path, args.kind)
        for error in inspection.errors:
            print(f"{path}: {error}", file=sys.stderr)
        valid += inspection.valid
    print(f"{valid}/{len(args.files)} valid", file=sys.stderr)
    return EXIT_OK if valid == len(args.files) else EXIT_INVALID


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bx", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="execute one work order (schemas/work-order/0.1.0)")
    p.add_argument("order")
    p.add_argument("--out", help="directory for result files (default ./results)")
    p.add_argument("--no-ingest", action="store_true",
                   help="only write the result files; do not deliver them to the local store")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("bench", help="benchmark functions of a Python module with pyperf",
                       description="bx bench MODULE [FUNCTION ...]: each FUNCTION is a case. With none, the "
                                   "module's bench_* and benchmark_* functions. Precision comes from "
                                   ".benchx/config.json (bx config init writes one) and the flags below; "
                                   "flags win, and pyperf's defaults fill the rest.")
    p.add_argument("module", help="a Python file; its tree is its git checkout, or --tree")
    p.add_argument("functions", nargs="*", metavar="FUNCTION")
    p.add_argument("--tree", help="the tree being measured (default: the module's git checkout or directory)")
    p.add_argument("--processes", type=int, help="worker processes (pyperf default 20)")
    p.add_argument("--values", type=int, help="values per process (pyperf default 3)")
    p.add_argument("--min-time", type=float, help="adaptive calibration: minimum seconds per value")
    p.add_argument("--loops", type=int, help="fixed calibration: inner loops per value")
    p.add_argument("--warmups", type=int, help="warmup values per process (0: none)")
    p.add_argument("-e", "--env", action="append", metavar="NAME=VALUE",
                   help="set an environment variable for the benchmark (repeatable)")
    p.add_argument("--project", help="name a project, so results join its series; omit for an ad hoc run")
    p.add_argument("--run-key", help="default: bench-<UTC timestamp>")
    p.add_argument("--out", help="directory for the order and result files (default ./results/RUN_KEY)")
    p.add_argument("--case-timeout", type=float, default=bench_mod.CASE_TIMEOUT_S,
                   help="seconds allowed per function (default %(default)s)")
    p.add_argument("--no-ingest", action="store_true", help="only write result files; skip the local store")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("config", help="manage .benchx/config.json")
    p.add_argument("action", choices=["init"], help="init: write a template stating pyperf's defaults")
    p.add_argument("--dir", default=".", help="where to write .benchx/config.json (default .)")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("ingest", help="sweep result files and work orders into the local store")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("series", help="list series")
    p.add_argument("--workload")
    p.add_argument("--quantity")
    p.set_defaults(fn=cmd_series)

    p = sub.add_parser("history", help="one series' points (a query; history mode is deferred)")
    p.add_argument("series", help="series fingerprint or prefix")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("head", help="the latest results in the store, newest first")
    p.add_argument("-n", type=int, default=10, help="how many (default 10)")
    p.add_argument("--json", action="store_true", help="print the stored documents instead")
    p.set_defaults(fn=cmd_head)

    p = sub.add_parser("target", help="ask for a target's description (schemas/target-description/0.1.0)",
                       description="TARGET is @CONFIG[=SOURCE_REF], asking the provider named in "
                                   ".benchx/provider.json, or a build directory holding .benchx-target.json. "
                                   "describe only reports a target that exists; prepare may build it.")
    p.add_argument("operation", choices=["describe", "prepare"])
    p.add_argument("target")
    p.set_defaults(fn=cmd_target)

    p = sub.add_parser("compare", help="compare two sides: read mode (--run) or measure mode (two targets)",
                       description="Read mode compares results already measured: --run KEY --baseline VALUE. "
                                   "Measure mode runs two prepared build directories, alternating them over "
                                   "--rounds rounds, delivers the results to the local store, then compares: "
                                   "BASELINE CONTENDER --suite NAME --rounds R, each target BUILD_DIR[:SOURCE_DIR] "
                                   "or @CONFIG[=SOURCE_REF] (the project's target provider).")
    p.add_argument("targets", nargs="*", metavar="TARGET", help="measure mode: BASELINE CONTENDER")
    p.add_argument("--profile", required=True, choices=["revisions", "environments"])
    p.add_argument("--k", type=float, default=3.0)
    p.add_argument("--min-rounds", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.add_argument("--label", help="read mode: the label that names the sides (environments); "
                                   "measure mode: NAME=BASE,CONTENDER")
    read = p.add_argument_group("read mode")
    read.add_argument("--run", help="run key")
    read.add_argument("--baseline", help="revisions: a revision or tree id prefix; environments: the label value")
    read.add_argument("--results", nargs="+",
                      help="compare these result directories through a throwaway store, not the local one")
    measure = p.add_argument_group("measure mode")
    measure.add_argument("--suite", help="the benchmark binary in each build directory")
    measure.add_argument("--filter", help="a Google Benchmark filter regex")
    measure.add_argument("--quantity", help="wall-time (default) or cpu-time")
    measure.add_argument("--rounds", type=int, help="alternation rounds; each runs both sides once")
    measure.add_argument("--repetitions", type=int, help="repetitions per case (default 5)")
    measure.add_argument("--min-time", type=float, help="minimum sample seconds (harness default if omitted)")
    measure.add_argument("--project", help="name a project, so the results join its series; omit for an ad hoc run")
    measure.add_argument("--run-key", help="default: compare-<UTC timestamp>")
    measure.add_argument("--out", help="directory for orders and result files (default ./results/RUN_KEY)")
    measure.add_argument("--no-build", action="store_true",
                         help="ask a provider to describe each @CONFIG target instead of preparing (building) it")
    measure.add_argument("--source-uri", help="the source's URI when the sides' checkouts do not share one")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("validate", help="check documents against their schema and rules",
                       description="Report every problem in each document, or nothing when all are valid.")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--kind", choices=get_args(Kind), default="measurement-result",
                   help="kind of document being checked (default: %(default)s)")
    p.set_defaults(fn=cmd_validate)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
