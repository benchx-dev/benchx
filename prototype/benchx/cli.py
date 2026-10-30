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
from . import core, harnesses, orders, runner, session
from . import target as target_mod
from .core.validation import Kind
from .store import Store

EXIT_OK = 0
EXIT_INVALID = 1  # a document invalid, a result rejected, or a target without a description
EXIT_REFUSED = 2  # the command was refused or could not be carried out
ASV_CASE_TIMEOUT_S = 1800


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


def _environment(items) -> dict:
    """`-e NAME=VALUE`, repeated, as a mapping; UsageError on a malformed item."""
    env = {}
    for item in items or []:
        name, eq, value = item.partition("=")
        if not (name and eq):
            raise UsageError(f"-e wants NAME=VALUE, not {item!r}")
        env[name] = value
    return env


def _bench_target(args) -> tuple[str, str, str | None]:
    """The target, suite and adapter `bx bench` runs. With `--suite` they are as given.
    Without it, the target is a Python module (the shorthand `bx bench MODULE.py
    [FUNCTION ...]`): its checkout is the target, its path in it the suite, and the
    harness pyperf. With `--adapter asv` it is an asv suite instead (`bx bench PATH
    --adapter asv`): PATH holds asv.conf.json or is the benchmark directory."""
    if args.suite:
        if args.tree:
            raise UsageError("--tree belongs to the shorthands; give the tree as the TARGET with --suite")
        return args.target, args.suite, args.adapter
    if args.adapter == "asv":
        return (*bench_mod.asv_target(args.target, args.tree), "asv")
    if not args.target.endswith(".py"):
        raise UsageError("give --suite, a Python file to benchmark, or an asv suite with --adapter asv")
    if args.adapter not in (None, "pyperf"):
        raise UsageError(f"a Python file is benchmarked with pyperf, not {args.adapter}")
    target, suite = bench_mod.module_target(args.target, args.tree)
    return target, suite, "pyperf"


def cmd_bench(args):
    """Run one target once, as one order of a comparison session: resolve the target,
    build the order from the harness defaults, config file, flags and environment
    variables, run it, deliver, print a table."""
    try:
        env = _environment(args.env)
        if args.functions and args.filter:
            raise UsageError("give case names or --filter, not both")
        target, suite, adapter = _bench_target(args)
    except UsageError as e:
        print(f"bx bench: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except bench_mod.SessionError as e:
        print(f"bx bench: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    store = None if args.no_ingest else Store()
    # An asv case is every round of its benchmark, each bounded by the benchmark's own timeout.
    case_s = (args.case_timeout if args.case_timeout is not None
              else ASV_CASE_TIMEOUT_S if adapter == "asv" else orders.CASE_TIMEOUT_S)
    try:
        harness = harnesses.get(adapter) if adapter else session.HARNESS
        names = harnesses.exact_filter(args.functions, harness.NAMES_TAKE_ARGUMENTS)
        summary = session.bench_target(
            target, suite=suite, store=store, adapter=adapter, flags={name: getattr(args, name) for name in harnesses.FLAG_NAMES},
            environment_variables=env, filter=names or args.filter,
            quantity=args.quantity or "wall-time", project=args.project, run_key=args.run_key, out=args.out,
            source_uri=args.source_uri, case_s=case_s, build=not args.no_build,
            progress=lambda message: print(message, file=sys.stderr))
    except (session.SessionError, config_mod.ConfigError) as e:
        print(f"bx bench: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except runner.Refused as e:
        return _refused(e)
    print(bench_mod.render(bench_mod.summarize(summary["files"])))
    print(f"\n{summary['results']} result(s) -> {summary['out']}  (order {summary['order'][:19]})")
    if store is None:
        return EXIT_OK
    print(f"{store.path}: ", end="")
    return _report(summary["outcome"])


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
        changed = f"  changed: {', '.join(p['changed'])}" if p["changed"] else ""
        print(f"{p['revision'][:12]}  {p['started_at']}  {p['value']:.6g} ({p['source']})  {p['ingest_key']}{changed}")
    return EXIT_OK


def cmd_head(args):
    store = Store()
    total, rows = store.head(args.n, args.project, args.workload, measurands=args.table)
    if args.json:
        for row in rows:
            print(row["document"])
        return EXIT_OK
    print(f"{store.path}: {total} result(s); latest {len(rows)}, newest first")
    if args.table:  # the columns of `bx bench`, led by where each result came from; measurands only
        print(bench_mod.render([{**bench_mod.row_of(json.loads(r["document"])), "revision": r["revision"][:10],
                                 "run": r["run_key"], "project": r["project"]} for r in rows],
                               leading=("revision", "run", "project")))
        return EXIT_OK
    cells = []
    for r in rows:
        median = f"{r['median']:.6g} {r['unit']}" if r["median"] is not None else "-"
        started = r["started_at"].strftime("%Y-%m-%d %H:%M:%S")
        labels = f"  {r['labels']}" if r["labels"] else ""
        cells.append((f"{r['row']:>5}", started, r["run_key"], r["workload"], r["quantity"], r["status"], median,
                      r["revision"][:10], f"{r['project']}{labels}"))
    widths = [max([len(cell[i]) for cell in cells], default=0) for i in range(len(cells[0]))] if cells else []
    for cell in cells:
        print("  ".join(text.ljust(width) for text, width in zip(cell, widths)).rstrip())
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
_MEASURE_ONLY = ("adapter", "suite", "filter", "quantity", "rounds", "env", *harnesses.FLAG_NAMES, "project",
                 "run_key", "out", "source_uri", "no_build")
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
            adapter=args.adapter, flags={name: getattr(args, name) for name in harnesses.FLAG_NAMES},
            environment_variables=_environment(args.env), filter=args.filter,
            quantity=args.quantity or "wall-time", project=args.project, label=label, run_key=args.run_key,
            out=args.out, source_uri=args.source_uri, k=args.k, min_rounds=args.min_rounds,
            build=not args.no_build, progress=lambda message: print(message, file=sys.stderr))
    except runner.Refused as e:
        return _refused(e)
    except UsageError as e:
        print(f"bx compare: error: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except (session.SessionError, compare_mod.CompareError, config_mod.ConfigError) as e:
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


def _add_target_options(parser, command):
    """The options `bx bench` and `bx compare` (measure mode) share: the harness,
    its suite and cases, its precision flags, and where the results go."""
    parser.add_argument("--adapter", choices=sorted(harnesses.HARNESSES),
                        help=f"the harness (default {session.HARNESS.name})")
    parser.add_argument("--suite", help="pyperf: the module's path in the working tree; "
                                        "google-benchmark: the binary in the build directory")
    parser.add_argument("--filter", help="a case filter regex (pyperf: function names; "
                                         "google-benchmark: --benchmark_filter)")
    parser.add_argument("--quantity", help="wall-time (default); google-benchmark also cpu-time")
    parser.add_argument("-e", "--env", action="append", metavar="NAME=VALUE",
                        help="set an environment variable for the benchmark (repeatable); "
                             "each becomes a workload parameter")
    parser.add_argument("--repetitions", type=int,
                        help=f"google-benchmark: repetitions per case (default {harnesses.GOOGLE_BENCHMARK.DEFAULT_REPETITIONS})")
    parser.add_argument("--processes", type=int, help="pyperf: worker processes (default 20)")
    parser.add_argument("--values", type=int, help="pyperf: values per process (default 3)")
    parser.add_argument("--min-time", type=float, help="adaptive calibration: minimum seconds per sample")
    parser.add_argument("--loops", type=int, help="pyperf: fixed calibration, inner loops per value")
    parser.add_argument("--warmups", type=int, help="pyperf: warmup values per process (0: none)")
    parser.add_argument("--project", help="name a project, so the results join its series; omit for an ad hoc run")
    parser.add_argument("--run-key", help=f"default: {command}-<UTC timestamp>")
    parser.add_argument("--out", help="directory for orders and result files (default ./results/RUN_KEY)")
    parser.add_argument("--no-build", action="store_true",
                        help="ask a provider to describe each @CONFIG target instead of preparing (building) it")
    parser.add_argument("--source-uri", help="the source's URI (default: the checkout's origin remote)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bx", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="execute one work order (schemas/work-order/0.1.0)")
    p.add_argument("order")
    p.add_argument("--out", help="directory for result files (default ./results)")
    p.add_argument("--no-ingest", action="store_true",
                   help="only write the result files; do not deliver them to the local store")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("bench", help="benchmark one target: the single-target form of bx compare",
                       description="bx bench TARGET --suite S [FUNCTION ...]: run one target once and print a table "
                                   "per case. TARGET and the options are those of bx compare: @CONFIG[=SOURCE_REF] "
                                   "(the project's target provider) or a path, which the harness reads (pyperf: a "
                                   "working tree; google-benchmark: BUILD_DIR[:SOURCE_DIR]). Each FUNCTION is an "
                                   "exact case name; with none, the harness's default cases. The shorthand "
                                   "bx bench MODULE.py [FUNCTION ...] benchmarks a Python module with pyperf: "
                                   "its checkout is the target, and its path there the suite. The shorthand "
                                   "bx bench PATH --adapter asv runs an asv suite, timed by pyperf: PATH holds "
                                   "asv.conf.json (its benchmark_dir is the suite) or is the benchmark directory; "
                                   "--filter searches name(p0, p1) per parameter combination, as asv run --bench, "
                                   "and the precision flags left out come from each benchmark's own attributes. "
                                   "Precision comes "
                                   "from the harness's defaults, the config file (pyperf; bx config init "
                                   "writes one) and the flags: flags win.")
    p.add_argument("target", metavar="TARGET",
                   help="@CONFIG[=SOURCE_REF], a path, or (with no --suite) a Python file or an asv suite")
    p.add_argument("functions", nargs="*", metavar="FUNCTION",
                   help="exact case names (default: all cases); asv: the name of a parametrized "
                        "benchmark selects all its combinations")
    _add_target_options(p, "bench")
    p.add_argument("--tree", help="the shorthand's tree (default: the module's or suite's git checkout or directory)")
    p.add_argument("--case-timeout", type=float,
                   help=f"seconds before one case is stopped (default {orders.CASE_TIMEOUT_S}; asv: "
                        f"{ASV_CASE_TIMEOUT_S}, all rounds, each also stopped at the benchmark's own timeout)")
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
    p.add_argument("--project", help="only this store project's results (e.g. numpy, local/HOST)")
    p.add_argument("--workload", help="only results whose benchmark name contains this")
    shape = p.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="print the stored documents instead")
    shape.add_argument("--table", action="store_true",
                       help="the `bx bench` table (median, mean ± sd, range, n, status) with revision, run and project")
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
                                   "Measure mode runs two prepared targets, alternating them over "
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
    measure.add_argument("--rounds", type=int, help="alternation rounds; each runs both sides once")
    _add_target_options(measure, "compare")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("validate", help="check documents against their schema and rules",
                       description="Report every problem in each document, or nothing when all are valid.")
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument("--kind", choices=get_args(Kind), default="measurement-result",
                   help="kind of document being checked (default: %(default)s)")
    p.set_defaults(fn=cmd_validate)

    args, extra = ap.parse_known_args(argv)
    # argparse takes a nargs="*" positional only from the first run of positionals, so cases
    # given after an option (`bx bench T --suite S CASE`) arrive here, on some Python versions.
    if args.cmd == "bench" and not any(item.startswith("-") for item in extra):
        args.functions += extra
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
