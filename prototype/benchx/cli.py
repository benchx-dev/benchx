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

from . import compare as compare_mod
from . import core, runner
from .core.validation import Kind
from .store import Store

EXIT_OK = 0
EXIT_INVALID = 1  # a document was invalid or a result rejected
EXIT_REFUSED = 2  # bx run refused the order, or bx compare could not compare


def cmd_run(args):
    """Run one order, write its result files, and deliver them to the local
    store. The runner itself stays store-unaware (runner.md §2 principle 6);
    this command composes it with ingest, as the workbench does."""
    out = Path(args.out) if args.out else Path("results")
    try:
        summary = runner.run(args.order, out)
    except runner.Refused as e:
        for problem in e.problems:
            print(f"refused: {problem}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"{summary['results']} result(s) for {summary['cases']} case(s) -> {summary['out']}"
          f"  (order {summary['order'][:19]})")
    if args.no_ingest:
        return EXIT_OK
    store = Store()
    print(f"{store.path}: ", end="")
    return _report(store.ingest_paths(summary["files"]))


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


def cmd_compare(args):
    if args.results:
        with tempfile.TemporaryDirectory() as tmp:  # the ad hoc path: a throwaway store
            store = Store(Path(tmp) / "store.parquet")
            outcome = store.ingest_paths(args.results)
            if outcome["rejected"]:
                return _report(outcome)
            return _compare(store, args)
    return _compare(Store(), args)


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

    p = sub.add_parser("run", help="execute one work order (#35 schema)")
    p.add_argument("order")
    p.add_argument("--out", help="directory for result files (default ./results)")
    p.add_argument("--no-ingest", action="store_true",
                   help="only write the result files; do not deliver them to the local store")
    p.set_defaults(fn=cmd_run)

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

    p = sub.add_parser("compare", help="run-mode comparison of two sides of one run")
    p.add_argument("--run", required=True, help="run key")
    p.add_argument("--profile", required=True, choices=["revisions", "environments"])
    p.add_argument("--baseline", required=True,
                   help="revisions: a revision or tree id prefix; environments: the label value")
    p.add_argument("--label", help="environments: the label that names the sides (required)")
    p.add_argument("--results", nargs="+",
                   help="compare these result directories through a throwaway store, not the local one")
    p.add_argument("--k", type=float, default=3.0)
    p.add_argument("--min-rounds", type=int, default=3)
    p.add_argument("--json", action="store_true")
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
