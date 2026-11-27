"""Terminal renderers for derived view marts (`bx view --cli`).

Display only (`system-decomposition.md` §3.9): renders the heuristic commit-to-commit
changes, movers, latest series summaries, and measured commits already computed by
`benchx.views.marts`, without computing verdicts.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from .. import tables as tables_mod
from . import inputs

CLI_VIEWS = ("changes", "movers", "latest", "commits")


def _parse_params(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _case_label(row: dict, *, show_quantity: bool = False) -> str:
    params = _parse_params(row.get("params") or "")
    pstr = ", ".join(f"{k}={v}" for k, v in params.items())
    label = str(row.get("case") or "") + (f" [{pstr}]" if pstr else "")
    qty = row.get("quantity") or ""
    if show_quantity and qty:
        label += f" ({qty})"
    return label


def _pct(value: float | None) -> str:
    return f"{value * 100:+.2f}%" if value is not None else "-"


def _short(sha: str | None) -> str:
    if not sha:
        return "-"
    short = inputs.short_sha(sha)
    return short[:8] if "+dirty-" not in short else short


def _fmt_date(value) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)[:16] if value else "-"


def _resolve_commit_ref(ref: str, commits: list[dict], repo: Path | None) -> set[str]:
    """Resolve a commit SHA prefix or git ref against measured commits."""
    shas = {c["sha"] for c in commits}
    matched = {s for s in shas if s.startswith(ref)}
    if matched:
        return matched
    git_repo = inputs._find_git_repo(repo)
    if git_repo is not None:
        proc = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            full = proc.stdout.strip()
            matched = {s for s in shas if s == full or s.startswith(full + "-dirty-")}
    return matched


def _compile_workload(pattern: str | None) -> re.Pattern | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise inputs.ViewError(f"invalid --workload regex {pattern!r}: {exc}") from None


def _matches_workload(row: dict, regex: re.Pattern | None) -> bool:
    if regex is None:
        return True
    haystacks = (
        str(row.get("suite") or ""),
        str(row.get("case") or ""),
        _case_label(row),
    )
    return any(regex.search(text) for text in haystacks)


def _serialize(row: dict) -> dict:
    return {
        k: (v.isoformat() if isinstance(v, dt.datetime) else v)
        for k, v in row.items()
        if k != "has_log"
    }


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]], *, right: set[int] = frozenset()) -> str:
    widths = [max(len(col) for col in [header[i], *(r[i] for r in rows)]) for i in range(len(header))]

    def fmt(cells: tuple[str, ...]) -> str:
        parts = [
            cell.rjust(w) if i in right else cell.ljust(w)
            for i, (cell, w) in enumerate(zip(cells, widths))
        ]
        return "  ".join(parts).rstrip()

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(header), sep, *(fmt(r) for r in rows)])


def _apply_limit(rows: list, limit: int | None) -> list:
    if limit is None:
        return rows
    if limit < 0:
        raise inputs.ViewError(f"--limit must be non-negative, not {limit}")
    return rows[:limit]


def render_cli(
    marts: dict,
    view: str = "changes",
    *,
    commit: str | None = None,
    workload: str | None = None,
    limit: int | None = None,
    as_json: bool = False,
    repo: Path | None = None,
) -> str:
    """Render one derived mart (`changes`, `movers`, `latest`, or `commits`) for the terminal."""
    if view not in CLI_VIEWS:
        raise inputs.ViewError(f"unsupported CLI view {view!r}; expected one of {', '.join(CLI_VIEWS)}")

    commits = marts["commits"]
    commit_shas = _resolve_commit_ref(commit, commits, repo) if commit is not None else None
    regex = _compile_workload(workload)

    if view in ("changes", "movers"):
        source_rows = list(marts[view])
        if view == "changes":
            source_rows.sort(
                key=lambda r: (-r["commit_order"], r["suite"], r["case"], r["params"], r["quantity"], r["node"])
            )
        rows = [
            r for r in source_rows
            if (commit_shas is None or r["sha"] in commit_shas) and _matches_workload(r, regex)
        ]
        rows = _apply_limit(rows, limit)
        if as_json:
            return json.dumps([_serialize(r) for r in rows], indent=2)
        if not rows:
            return f"(no {view} to display)"
        quantities = {r["quantity"] for r in rows}
        nodes = {r["node"] for r in rows}
        show_qty = len(quantities) > 1 or quantities != {"wall-time"}
        show_node = len(nodes) > 1
        header = ("commit", *("node",) * show_node, "case", "before", "after", "change")
        body = []
        for r in rows:
            pair = f"{_short(r['prev_sha'])}->{_short(r['sha'])}"
            before = tables_mod.human(r["prev_estimate"], None, r["unit"])
            after = tables_mod.human(r["estimate"], None, r["unit"])
            cells = (
                pair,
                *((r["node"],) if show_node else ()),
                _case_label(r, show_quantity=show_qty),
                before,
                after,
                _pct(r["rel_change"]),
            )
            body.append(cells)
        n_cols = len(header)
        right = {n_cols - 3, n_cols - 2, n_cols - 1}
        note = f"{view} (heuristic; not a comparator verdict — use `bx compare` for verdicts)"
        return note + "\n" + _table(header, body, right=right)

    if view == "latest":
        rows = [
            r for r in marts["latest"]
            if (commit_shas is None or r["sha"] in commit_shas) and _matches_workload(r, regex)
        ]
        rows = _apply_limit(rows, limit)
        if as_json:
            return json.dumps([_serialize(r) for r in rows], indent=2)
        if not rows:
            return "(no series to display)"
        quantities = {r["quantity"] for r in rows}
        nodes = {r["node"] for r in rows}
        show_qty = len(quantities) > 1 or quantities != {"wall-time"}
        show_node = len(nodes) > 1
        header = ("commit", *("node",) * show_node, "case", "estimate", "vs_prev", "vs_10", "noise", "pts")
        body = []
        for r in rows:
            est = tables_mod.human(r["estimate"], None, r["unit"])
            noise = f"{r['noise'] * 100:.2f}%" if r["noise"] is not None else "-"
            cells = (
                _short(r["sha"]),
                *((r["node"],) if show_node else ()),
                _case_label(r, show_quantity=show_qty),
                est,
                _pct(r["vs_prev"]),
                _pct(r["vs_10"]),
                noise,
                str(r["points"]),
            )
            body.append(cells)
        n_cols = len(header)
        right = {n_cols - 5, n_cols - 4, n_cols - 3, n_cols - 2, n_cols - 1}
        return _table(header, body, right=right)

    # view == "commits"
    rows = [
        r for r in commits
        if commit_shas is None or r["sha"] in commit_shas
    ]
    rows = _apply_limit(rows, limit)
    if as_json:
        return json.dumps([_serialize(r) for r in rows], indent=2)
    if not rows:
        return "(no commits to display)"
    header = ("commit", "date", "node", "state", "results", "subject")
    body = [
        (
            _short(r["sha"]),
            _fmt_date(r.get("committer_date") or r.get("started_at")),
            r["node"],
            r["state"],
            str(r["results"]),
            (r["subject"] or "")[:72],
        )
        for r in rows
    ]
    return _table(header, body, right={4})
