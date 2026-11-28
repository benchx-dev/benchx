"""`bx bench`: one target, one order, one table (prototype-design.md §1).

`bx bench` is the single-target form of `bx compare`: `session.bench_target`
resolves the target and builds the order the way a comparison session does
(no round, no slot, no comparison). What is left here is the shorthand
`bx bench MODULE.py [FUNCTION ...]`, which names a Python module's checkout as
the target and the module's path in it as the suite, its asv counterpart
`bx bench PATH --adapter asv`, and the table that summarizes the results.
"""

import json
import logging
import statistics
from pathlib import Path

from . import core, snapshot
from .errors import SessionError
from .projection import declared_workload, said, workload_label
from .results import warning_codes

logger = logging.getLogger(__name__)


def tree_of(module: Path, tree=None) -> Path:
    """The tree being measured: --tree, else the module's git checkout, else its directory."""
    if tree:
        logger.debug("the shorthand's tree is %s, from --tree", Path(tree).resolve())
        return Path(tree).resolve()
    top = snapshot._run(["git", "rev-parse", "--show-toplevel"], cwd=module.parent)
    if top:
        logger.debug("the shorthand's tree is %s, the git checkout of %s", Path(top).resolve(), module)
        return Path(top).resolve()
    logger.info("no git checkout: the directory %s is the tree", module.parent.resolve())
    return module.parent.resolve()


def module_target(module, tree=None) -> tuple[str, str]:
    """The shorthand's target and suite: the module's tree, and the module's path in it."""
    module = Path(module).resolve()
    if not module.is_file():
        raise SessionError(f"no such module: {module}")
    root = tree_of(module, tree)
    try:
        suite = module.relative_to(root).as_posix()
    except ValueError:
        raise SessionError(f"{module} is not inside the tree {root}; give --tree") from None
    logger.debug("the shorthand's suite is %s", suite)
    return str(root), suite


def asv_target(path, tree=None) -> tuple[str, str]:
    """The asv shorthand's target and suite. `path` is a directory holding
    asv.conf.json (its `benchmark_dir`, default `benchmarks`, is the suite) or the
    benchmark directory itself; the target is its tree, as for a module."""
    from .adapters import asv

    benchmark_dir = Path(path).resolve()
    for name in asv.CONF_NAMES:
        if (benchmark_dir / name).is_file():
            conf = asv._read_conf(benchmark_dir / name)
            logger.debug("read %s: benchmark_dir %s", benchmark_dir / name, conf.get("benchmark_dir", "benchmarks"))
            benchmark_dir = (benchmark_dir / conf.get("benchmark_dir", "benchmarks")).resolve()
            break
    if not benchmark_dir.is_dir():
        raise SessionError(f"no such benchmark directory: {benchmark_dir}")
    root = tree_of(benchmark_dir, tree)
    try:
        return str(root), benchmark_dir.relative_to(root).as_posix()
    except ValueError:
        raise SessionError(f"{benchmark_dir} is not inside the tree {root}; give --tree") from None


def summarize(files) -> list[dict]:
    """One row per result document among `files`."""
    rows = []
    for path in files:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if document.get("schema_version") == core.catalog.WORK_ORDER:
            continue
        if document["measurement"].get("kind") == "condition":
            continue  # the runner's load-average sibling: a condition, not a case (schema §4.4)
        rows.append(row_of(document))
    return rows


def row_of(document: dict) -> dict:
    """The table row of one result document."""
    measurement = document["measurement"]
    # An observation is a number, or an object carrying one with its metadata (pyperf's).
    values = [o["value"] if isinstance(o, dict) else o for o in measurement.get("observations", [])]
    return {
        "case": workload_label(declared_workload(document)),
        "status": measurement["status"] + (f" ({measurement['reason']})" if measurement.get("reason") else ""),
        "n": len(values),
        "median": statistics.median(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "stdev": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "loops": document.get("procedure", {}).get("inner_iterations"),
        "warnings": warning_codes(document),
        "message": None if measurement["status"] == "success" else said(document),
    }


def _time(seconds) -> str:
    if seconds is None:
        return "-"
    for scale, unit in ((1, "s"), (1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if seconds >= scale:
            return f"{seconds / scale:.4g} {unit}"
    return f"{seconds / 1e-9:.4g} ns"


def render(rows, leading=()) -> str:
    """The table; `leading` names row keys to show as columns before the case."""
    header = (*leading, "case", "median", "mean ± sd", "min .. max", "n", "status")
    body = [
        (
            *(str(r[key]) for key in leading),
            r["case"],
            _time(r["median"]),
            f"{_time(r['mean'])} ± {_time(r['stdev'])}" if r["mean"] is not None else "-",
            f"{_time(r['min'])} .. {_time(r['max'])}" if r["min"] is not None else "-",
            str(r["n"]),
            r["status"] + (f"  [{', '.join(r['warnings'])}]" if r["warnings"] else ""),
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in [header, *body]]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def failures(rows, names=3) -> str:
    """The section under the table for the cases that were not measured: what each said, `case: message`,
    cases with the same message as one line, `message (K cases: a, b, c, …)`. Empty when none has a message
    (the status column is then all there is). A row's message is one line already (`projection.said`)."""
    said_by = {}
    for row in rows:
        if row.get("message"):
            said_by.setdefault(row["message"], []).append(row["case"])
    if not said_by:
        return ""
    lines = [
        f"  {message} ({len(cases)} cases: {', '.join(cases[:names])}{', …' if len(cases) > names else ''})"
        if len(cases) > 1
        else f"  {cases[0]}: {message}"
        for message, cases in said_by.items()
    ]
    return "\n".join(["", f"not measured ({sum(len(c) for c in said_by.values())})", *lines])
