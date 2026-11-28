"""asv-style tables for `bx compare --tables` (TABLES_PLAN.md): one table per side, then the changes.

Display only. The per-side numbers are recomputed here from the run's stored results, with the
projection that made the comparison document, so this module adds nothing to the document. (The
document gains, in compare.py, an optional `attempts` for each unit and the reason `not-measured`,
which the change table shows; see comparator.md.) A side's value is the median of its per-round
medians (the estimator the verdict uses) and its `±` the sd of those medians: benchx's own meaning,
not asv's.
"""

import collections
import logging
import math
import statistics

from . import identity
from .projection import estimate, values
from .results import store_project
from .run_profile import WORKLOAD, _c, _run_documents, _unit_order, _workload, profile_projection
from .snapshot import DIRTY_WARNINGS

logger = logging.getLogger(__name__)

ROLES = ("baseline", "contender")
CHANGED = {"regressed": "+", "improved": "-", "changed": "~"}  # as asv continuous marks them
MARKS = {**CHANGED, "no-change-detected": " ", "equivalent": " ", "indeterminate": "?"}
SHA = 8  # asv's width
# asv_runner.util._human_time_units: the unit is the largest one the value does not reach.
TIME_UNITS = (
    ("ns", 1e-9),
    ("μs", 1e-6),
    ("ms", 1e-3),
    ("s", 1),
    ("m", 60),
    ("h", 3600),
    ("d", 86400),
    ("w", 604800),
    ("M", 2620800),
    ("q", 7862400),
    ("y", 31449600),
    ("C", 3144960000),
)


def _float(value: float, significant: int, truncate_small: int | None = None, zeros: bool = False) -> str:
    """`value` to `significant` digits, as asv's `human_float`: scientific notation outside 1e-5 and 1e9;
    trailing zeros dropped unless `zeros`, which also pads `3` to `3.00`; with `truncate_small`, the
    zeros after the point of a number below 1 count as digits, up to that many."""
    if value == 0:
        return "0"
    if math.isinf(value) or math.isnan(value):
        return str(value)
    sign, value = ("-", -value) if value < 0 else ("", value)
    magnitude = math.floor(math.log10(value)) + 1
    if truncate_small is not None:
        magnitude = max(magnitude, 1 - truncate_small)
    decimals = significant - magnitude
    scientific = magnitude <= -5 or magnitude >= 9
    if scientific:
        text = f"{value:.{significant}e}"
    elif value == int(value) or decimals <= 0:
        text = str(int(round(value, decimals)))
    else:
        text = f"{value:.{decimals}f}"
    if not zeros and "." in text and not scientific:
        text = text.rstrip("0").removesuffix(".")
    text = sign + text
    if zeros and "." not in text and len(text) < significant:
        text += "." + "0" * (significant - len(text))
    return text


def human(value, spread, unit) -> str:
    """`4.71±0.08μs`, as asv writes a time (`human_time`): the value to 3 digits with its zeros, the
    spread to 1. Any other unit is written after the number."""
    if value is None or value != value:
        return "n/a"
    if unit != "s":
        return _float(value, 3) + (f"±{_float(spread, 1, 2)}" if spread is not None else "") + f" {unit}"
    scale = spread if value == 0 and spread is not None else value
    units = TIME_UNITS if scale != 0 else (("s", 1), ("m", 60))
    for (name, size), (_, bigger) in zip(units, units[1:]):
        if scale < bigger:
            return (
                _float(value / size, 3, zeros=True)
                + (f"±{_float(spread / size, 1, truncate_small=2)}" if spread is not None else "")
                + name
            )
    return "~0"


def _projection(doc):
    """The projection that made `doc`, from the parameters its selector recorded."""
    params = doc["selector"]["params"]
    return profile_projection(params["profile"], params.get("label"), params.get("parameter"))


def _side(docs) -> dict:
    """One side of one unit: the median of its per-round medians, their sd, and what went wrong if
    no round succeeded."""
    medians = [
        m
        for d in docs
        if d["measurement"]["status"] == "success" and (m := estimate(identity.MEDIAN, values(d))) is not None
    ]
    failed = sorted({d["measurement"]["status"] for d in docs if d["measurement"]["status"] != "success"})
    return {
        "value": statistics.median(medians) if medians else None,
        "spread": statistics.stdev(medians) if len(medians) > 1 else None,
        "status": "ok" if medians else (failed[0] if failed else "n/a"),
    }


def summarize(doc, documents) -> tuple[list[dict], dict]:
    """The comparison's units, sorted as the document lists them, each with its workload, quantity and
    the two sides' numbers; and what each side is (commit, project, rounds, dirty)."""
    projection, params, named = _projection(doc), doc["selector"]["params"], doc["sides"]
    names = {_c(named["baseline"]): "baseline", _c(named["contender"]): "contender"}
    grouped, sides = {}, {role: [] for role in ROLES}
    skipped = 0
    for d in _run_documents(documents, params["run_key"]):
        projected = projection.apply(d, store_project(d))
        role = names.get(_c(projected.axis[0]))
        if role is None:
            skipped += 1
            continue
        variant = {ptr: v for ptr, v in projected.identity.items() if ptr == WORKLOAD or ptr.startswith(WORKLOAD + "/")}
        quantity = d["measurement"]["quantity"]
        unit = grouped.setdefault(
            (_c(variant), quantity["name"]), {"quantity": quantity, "docs": {r: [] for r in ROLES}}
        )
        unit["docs"][role].append(d)
        sides[role].append(d)
    if skipped:
        logger.debug("--tables skipped %d document(s) that fit neither side", skipped)
    if len(grouped) != len(doc["units"]):  # side tables regroup every attempt: units the comparison dropped show up
        logger.debug(
            "the side tables hold %d unit(s), the comparison %d: units dropped for sides-agree, "
            "equal-attempts or a missing side still appear in the side tables",
            len(grouped),
            len(doc["units"]),
        )
    units = []
    for unit in grouped.values():
        units.append(
            {
                "workload": _workload(unit["docs"], projection.axis),
                "quantity": unit["quantity"],
                **{role: _side(unit["docs"][role]) for role in ROLES},
            }
        )
    units.sort(key=lambda u: _unit_order(u["workload"], u["quantity"]["name"]))
    varying = named["varying"].rsplit(".", 1)[-1]  # the label or parameter that names the sides
    revisions = params["profile"] == "revisions"  # its sides are two commits; the others', two values
    info = {}
    for role, docs in sides.items():
        info[role] = {
            "project": store_project(docs[0]) if docs else "",
            "commit": docs[0]["revision"]["key"][:SHA] if docs else "?",
            "rounds": len({d.get("procedure", {}).get("round") for d in docs}),
            "dirty": any(d["provenance"].get(flag) == "dirty" for d in docs for flag, _ in DIRTY_WARNINGS),
        }
    if revisions and len({(side["commit"], side["dirty"]) for side in info.values()}) == 1:
        # The same commit on both sides, each with uncommitted changes: the tree id tells them apart.
        for role, side in info.items():
            side["commit"] += f" tree {str(named[role])[:SHA]}"
    for role, side in info.items():
        # Uncommitted changes against HEAD share HEAD's commit, so a side that is not clean says so.
        side["tag"] = side["commit"] + (" (dirty)" if side["dirty"] else "") if revisions else str(named[role])
        side["heading"] = (
            f"{side['project']} commit {side['commit']}" if revisions else f"{side['project']} {varying}={named[role]}"
        )
    return units, info


def _grid(headers, rows) -> list[str]:
    """asv's table (`format_text_table`): each column its widest cell plus 2, every cell centered, a
    space between columns, `=` borders and `-` under the header. Lines are not trimmed."""
    cells = [[str(cell) for cell in row] for row in (headers, *rows)]
    widths = [max(len(row[j]) for row in cells) + 2 for j in range(len(headers))]
    lines = [" ".join(cell.center(width) for cell, width in zip(row, widths)) for row in cells]
    rule = lambda char: " ".join(char * width for width in widths)  # noqa: E731
    return [rule("="), lines[0], rule("-"), *lines[1:], rule("=")]


def _cell(unit, role) -> str:
    """A side's value, or why it has none."""
    side = unit[role]
    return (
        human(side["value"], side["spread"], unit["quantity"].get("unit"))
        if side["value"] is not None
        else side["status"]
    )


def side_tables(units, role, info) -> str:
    """The tables of one side: a block per benchmark, a row per parameter combination."""
    side = info[role]
    lines = [f"For {side['heading']}" + (" [dirty tree]" if side["dirty"] else "") + f" ({side['rounds']} rounds):"]
    benchmarks = {}
    for unit in units:
        benchmarks.setdefault((unit["workload"]["name"], unit["quantity"]["name"]), []).append(unit)
    for (name, quantity), group in benchmarks.items():
        title = name + ("" if quantity == "wall-time" else f" ({quantity})")
        names = list(dict.fromkeys(p for u in group for p in (u["workload"].get("parameters") or {})))
        cell = lambda u: _cell(u, role)  # noqa: E731
        if not names:
            lines.append(f"{title}  {cell(group[0])}")
            continue
        bad = [u[role]["status"] for u in group if u[role]["status"] != "ok"]
        lines.append(f"{title}  {bad[0] if bad else 'ok'}")
        rows = [[(u["workload"].get("parameters") or {}).get(n, "") for n in names] + [cell(u)] for u in group]
        lines.extend("  " + line for line in _grid([*names, ""], rows))  # under the title
    return "\n".join(lines)


def _label(workload) -> str:
    """asv's `name(p1, p2)`."""
    parameters = workload.get("parameters") or {}
    return workload["name"] + (f"({', '.join(str(v) for v in parameters.values())})" if parameters else "")


def _pipe(headers, rows, right=()) -> str:
    """asv's `tabulate` "github" table: a column is as wide as its widest cell or its header plus 2;
    text is left-aligned, and the `right` columns (numbers) right-aligned."""
    widths = [max(len(header) + 2, *(len(row[j]) for row in rows)) for j, header in enumerate(headers)]
    align = lambda cell, j: cell.rjust(widths[j]) if j in right else cell.ljust(widths[j])  # noqa: E731
    line = lambda cells: "| " + " | ".join(align(cell, j) for j, cell in enumerate(cells)) + " |"  # noqa: E731
    return "\n".join(
        [line(headers), "|" + "|".join("-" * (width + 2) for width in widths) + "|", *(line(row) for row in rows)]
    )


def _verdict(unit) -> str:
    """The comparator's verdict in the words of its summary, with the reason when there is no verdict."""
    if unit.get("reason") == "benchmark-changed":  # as asv words it
        return "not comparable (benchmark-changed)"
    return unit["verdict"].replace("-", " ") + (f" ({unit['reason']})" if unit.get("reason") else "")


def change_table(doc, units, info, *, all_rows=False) -> str:
    """asv's comparison table, and the verdict. As `asv continuous` does, only the rows that
    changed, unless `all_rows`."""
    by_workload = {(_c(u["workload"]), u["quantity"]["name"]): u for u in units}
    rows, hidden = [], collections.Counter()
    for verdict_unit in doc["units"]:
        mark = "x" if verdict_unit.get("reason") == "benchmark-changed" else MARKS[verdict_unit["verdict"]]
        if mark not in (*CHANGED.values(), "x") and not all_rows:  # asv lists "not comparable" by default
            hidden[verdict_unit["verdict"]] += 1
            continue
        unit = by_workload[(_c(verdict_unit["workload"]), verdict_unit["quantity"])]
        before, after = unit["baseline"], unit["contender"]
        ratio = f"{after['value'] / before['value']:.2f}" if before["value"] and after["value"] is not None else "n/a"
        noise = f"{verdict_unit['details']['noise']:.2%}" if "noise" in verdict_unit["details"] else "n/a"
        rows.append(
            [
                mark,
                *(_cell(unit, role) for role in ROLES),
                ratio,
                noise,
                _verdict(verdict_unit),
                _label(unit["workload"]),
            ]
        )
    logger.info(
        "--tables: %d row(s) shown; hidden by verdict: %s",
        len(rows),
        ", ".join(f"{verdict} {n}" for verdict, n in sorted(hidden.items())) or "none",
    )
    if not rows:
        return "No benchmark changed (the verdict is benchx's; --all lists every row)."
    headers = [
        "Change",
        f"Before [{info['baseline']['tag']}]",
        f"After [{info['contender']['tag']}]",
        "Ratio",
        "Noise",
        "Verdict",
        "Benchmark (Parameter)",
    ]
    return _pipe(headers, rows, right={j for j in (3, 4) if all(row[j] != "n/a" for row in rows)})


def render(doc, documents, *, all_rows=False) -> str:
    """The tables of a comparison, or nothing when the document has no units (its invariants failed)."""
    if not doc.get("units") or "contender" not in doc.get("sides", {}):
        return ""
    units, info = summarize(doc, documents)
    return "\n" + "\n\n".join(
        [*(side_tables(units, role, info) for role in ROLES), change_table(doc, units, info, all_rows=all_rows)]
    )
