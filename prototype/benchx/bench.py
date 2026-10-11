"""`bx bench mod.py f g`: benchmark Python functions with pyperf (PYPERF_PLAN.md step 5).

The workbench's part for a one-off: build one work order from the module, the
config file, the flags and the environment variables, run it, deliver the
results to the local store, and summarize them as a table. Nothing is built or
checked out; the tree is measured as found.
"""

import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, core, runner, snapshot
from .session import CASE_TIMEOUT_S, ORDER_TIMEOUT_S, SessionError, _source_uri

ADAPTER = "pyperf"


def tree_of(module: Path, tree=None) -> Path:
    """The tree being measured: --tree, else the module's git checkout, else its directory."""
    if tree:
        return Path(tree).resolve()
    top = snapshot._run(["git", "rev-parse", "--show-toplevel"], cwd=module.parent)
    return Path(top).resolve() if top else module.parent.resolve()


def case_filter(functions) -> str | None:
    """Exact names as one regex; None leaves the adapter's default (bench_*, benchmark_*)."""
    return "^(" + "|".join(re.escape(f) for f in functions) + ")$" if functions else None


def flags_to_precision(processes=None, values=None, min_time=None, loops=None, warmups=None) -> dict:
    """The flags that set precision, spelled as a work-order `precision`."""
    if min_time is not None and loops is not None:
        raise SessionError("--min-time and --loops both set calibration; give one")
    precision = {}
    levels = [{"unit": unit, "n": n} for unit, n in (("process", processes), ("value", values)) if n is not None]
    if levels:
        precision["repetitions"] = {"mode": "fixed", "levels": levels}
    if min_time is not None:
        precision["calibration"] = {"mode": "adaptive", "minimum_sample_seconds": min_time}
    if loops is not None:
        precision["calibration"] = {"mode": "fixed", "n_iterations": loops}
    if warmups is not None:
        precision["warmup"] = {"mode": "count", "n_warmup": warmups} if warmups else {"mode": "none"}
    return precision


def build_order(module, functions, *, tree=None, precision_flags=None, environment_variables=None,
                project=None, run_key=None, labels=None, case_s=CASE_TIMEOUT_S, order_s=ORDER_TIMEOUT_S, cwd="."):
    """The resolved work order, and the config file it read (or None)."""
    module = Path(module).resolve()
    if not module.is_file():
        raise SessionError(f"no such module: {module}")
    root = tree_of(module, tree)
    try:
        suite = module.relative_to(root).as_posix()
    except ValueError:
        raise SessionError(f"{module} is not inside the tree {root}; give --tree") from None
    precision, config_path = config.resolve(ADAPTER, precision_flags, start=cwd)
    entry = {"adapter": ADAPTER, "suite": suite}
    if functions:
        entry["filter"] = case_filter(functions)
    run_key = run_key or "bench-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    order = {
        "schema_version": "benchx/work-order/0.1.0",
        "work_order_id": f"urn:uuid:{uuid.uuid4()}",
        "state": "resolved",
        "suites": [entry],
        "target": {"kind": "working_tree", "path": str(root), "source_dir": str(root),
                   "source": {"uri": _source_uri(str(root)), "type": "git"}},
        "quantities": ["wall-time"],
        "precision": precision,
        "timeouts": {"case_s": case_s, "order_s": order_s},
        "run_key": run_key,
        "requester": {"kind": "workbench", "name": "bx"},
        "plan": [{"id": "p0", "case": "*", "quantity": "wall-time"}],
    }
    if environment_variables:
        order["environment_variables"] = dict(environment_variables)
    if project:
        order["project"] = project
    if labels:
        order["labels"] = labels
    errors = core.check(order, "work-order")
    if errors:
        raise SessionError("; ".join(str(error) for error in errors))
    return order, config_path


def summarize(files) -> list[dict]:
    """One row per result document among `files`."""
    rows = []
    for path in files:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if document.get("schema_version") == "benchx/work-order/0.1.0":
            continue
        measurement = document["measurement"]
        values = [o["value"] for o in measurement.get("observations", [])]
        rows.append({
            "case": document["coordinates"]["workload"]["name"],
            "status": measurement["status"] + (f" ({measurement['reason']})" if measurement.get("reason") else ""),
            "n": len(values),
            "median": statistics.median(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "stdev": statistics.stdev(values) if len(values) > 1 else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "loops": document["procedure"].get("inner_iterations"),
            "warnings": [w.get("code", str(w)) if isinstance(w, dict) else str(w)
                         for w in document.get("quality", {}).get("warnings", [])],
        })
    return rows


def _time(seconds) -> str:
    if seconds is None:
        return "-"
    for scale, unit in ((1, "s"), (1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if seconds >= scale:
            return f"{seconds / scale:.4g} {unit}"
    return f"{seconds / 1e-9:.4g} ns"


def render(rows) -> str:
    header = ("case", "median", "mean ± sd", "min .. max", "n", "status")
    body = [(r["case"], _time(r["median"]),
             f"{_time(r['mean'])} ± {_time(r['stdev'])}" if r["mean"] is not None else "-",
             f"{_time(r['min'])} .. {_time(r['max'])}" if r["min"] is not None else "-",
             str(r["n"]), r["status"] + (f"  [{', '.join(r['warnings'])}]" if r["warnings"] else ""))
            for r in rows]
    widths = [max(len(row[i]) for row in [header, *body]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip() for row in [header, *body]]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def run_order(order, out_dir) -> dict:
    """Write the order under out_dir/orders and run it; the runner's summary."""
    import json
    orders = Path(out_dir) / "orders"
    orders.mkdir(parents=True, exist_ok=True)
    path = orders / "order.json"
    path.write_text(json.dumps(order, indent=1) + "\n")
    return runner.run(path, out_dir)
