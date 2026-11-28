"""Derive flat, query-shaped tables (marts) from runs and benchmark points.

Pure Python stdlib computation; PyArrow is imported lazily only when writing the
optional `.parquet` marts:

    commits.parquet   one row per measured commit x node, with ancestry and status
    points.parquet    one row per result, sorted by suite, case and commit order
    changes.parquet   every point against the previous point of its series
    latest.parquet    one row per series: latest estimate, changes, noise, rank
    movers.parquet    the largest changes per commit x node
    manifest.json     counts, the latest commit, the tables

A series is (suite, case, params, quantity, node). Both `success` and `partial`
results contribute estimates (matching `benchx.identity.points`), while `skipped`
is preserved as non-failing status. Verdicts are not computed here: the relative
changes are a heuristic until benchx's comparator history mode exists.
"""

from __future__ import annotations

import datetime as dt
import heapq
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .inputs import UTC, _require_pyarrow, short_sha, synthesize_runs

RECENT = 21  # points per series the noise estimate looks at (20 consecutive changes)
MOVERS_PER_COMMIT = 100
MEASURED_STATUSES = ("success", "partial")
NON_FAILED_STATUSES = ("success", "partial", "skipped")


def is_measured(point: dict) -> bool:
    """Whether a point carries a usable measurement estimate (matching identity.points)."""
    return point["status"] in MEASURED_STATUSES and point["estimate"] is not None


def is_failed_status(status: str | None) -> bool:
    """Whether a result status represents a failure (error/censored, not skipped/partial/success)."""
    return bool(status) and status not in NON_FAILED_STATUSES


def _series_key(row: dict) -> tuple:
    return (row["suite"], row["case"], row["params"], row["quantity"], row["node"])


def _order_shas(runs: list[dict]) -> dict[str, int]:
    """Order unique SHAs by committer_date while keeping every known parent before its child."""
    min_dt = dt.datetime.min.replace(tzinfo=UTC)
    date_by_sha: dict[str, dt.datetime] = {}
    parents_by_sha: dict[str, set[str]] = defaultdict(set)
    for run in runs:
        sha = run["sha"]
        cdate = run.get("committer_date") or min_dt
        if sha not in date_by_sha or cdate < date_by_sha[sha]:
            date_by_sha[sha] = cdate
        for p in run.get("parents") or ():
            if p and p != sha:
                parents_by_sha[sha].add(p)

    all_shas = set(date_by_sha)
    in_degree: dict[str, int] = {}
    children: dict[str, list[str]] = defaultdict(list)
    for sha in all_shas:
        known_parents = [p for p in parents_by_sha.get(sha, ()) if p in all_shas]
        in_degree[sha] = len(known_parents)
        for p in known_parents:
            children[p].append(sha)

    # Kahn's topological sort with a min-heap keyed on (committer_date, sha):
    # parents always precede their children even when timestamps tie within one second.
    ready: list[tuple[dt.datetime, str]] = [(date_by_sha[sha], sha) for sha, deg in in_degree.items() if deg == 0]
    heapq.heapify(ready)
    order: dict[str, int] = {}
    while ready:
        _, sha = heapq.heappop(ready)
        if sha in order:
            continue
        order[sha] = len(order) + 1
        for child in children.get(sha, ()):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                heapq.heappush(ready, (date_by_sha[child], child))

    # Fallback if synthetic metadata ever contains a cycle.
    if len(order) < len(all_shas):
        remaining = sorted((date_by_sha[sha], sha) for sha in all_shas if sha not in order)
        for _, sha in remaining:
            order[sha] = len(order) + 1
    return order


def build_commits(runs: list[dict]) -> list[dict]:
    """Order commits across all nodes by committer_date, breaking ties by ancestry then sha."""
    order = _order_shas(runs)
    rows: list[dict] = []
    for run in runs:
        parents = list(run.get("parents") or [])
        rows.append(
            {
                "sha": run["sha"],
                "short": short_sha(run["sha"]),
                "parents": parents,
                "parent": parents[0] if parents else None,
                "author_date": run.get("author_date"),
                "committer_date": run.get("committer_date"),
                "subject": run.get("subject") or "",
                "branch": run.get("branch") or "",
                "node": run["node"],
                "state": run.get("state") or "unknown",
                "reason": run.get("reason"),
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "suites_ok": int(run.get("suites_ok") or 0),
                "suites_failed": int(run.get("suites_failed") or 0),
                "results": int(run.get("results") or 0),
                "commit_order": order[run["sha"]],
                "has_log": bool(run.get("log")),
            }
        )
    rows.sort(key=lambda r: (-r["commit_order"], r["node"]))
    return rows


def build_points(raw_points: list[dict], commits: list[dict]) -> list[dict]:
    """Attach commit_order, committer_date, and parent to each point and sort."""
    by_pair = {(c["sha"], c["node"]): c for c in commits}
    rows: list[dict] = []
    for pt in raw_points:
        c = by_pair.get((pt["sha"], pt["node"]))
        rows.append(
            {
                "sha": pt["sha"],
                "commit_order": c["commit_order"] if c else 0,
                "committer_date": c["committer_date"] if c else pt.get("started_at"),
                "parent": (c["parent"] or "") if c else "",
                "node": pt["node"],
                "suite": pt["suite"],
                "case": pt["case"],
                "params": pt["params"],
                "quantity": pt["quantity"],
                "unit": pt["unit"],
                "direction": pt["direction"],
                "status": pt["status"],
                "reason": pt["reason"],
                "estimate": pt["estimate"],
                "mean": pt["mean"],
                "min": pt["min"],
                "max": pt["max"],
                "n": int(pt["n"]),
                "started_at": pt.get("started_at"),
                "run_key": pt["run_key"],
                "ingest_key": pt["ingest_key"],
            }
        )
    rows.sort(key=lambda r: (_series_key(r), r["commit_order"]))
    return rows


def build_changes(points: list[dict]) -> list[dict]:
    """Every measured point compared against the immediately preceding point in its series."""
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for pt in points:
        if is_measured(pt):
            grouped[_series_key(pt)].append(pt)

    rows: list[dict] = []
    for key in sorted(grouped):
        series_pts = sorted(grouped[key], key=lambda p: p["commit_order"])
        for prev, cur in zip(series_pts, series_pts[1:]):
            prev_est = prev["estimate"]
            rel = (cur["estimate"] / prev_est - 1.0) if prev_est > 0 else None
            rows.append(
                {
                    "sha": cur["sha"],
                    "commit_order": cur["commit_order"],
                    "committer_date": cur["committer_date"],
                    "parent": cur["parent"],
                    "node": cur["node"],
                    "suite": cur["suite"],
                    "case": cur["case"],
                    "params": cur["params"],
                    "quantity": cur["quantity"],
                    "unit": cur["unit"],
                    "direction": cur["direction"],
                    "estimate": cur["estimate"],
                    "prev_estimate": prev_est,
                    "prev_sha": prev["sha"],
                    "prev_order": prev["commit_order"],
                    "rel_change": rel,
                    "prev_is_parent": prev["sha"] == cur["parent"],
                    "gap": cur["commit_order"] - prev["commit_order"],
                }
            )
    rows.sort(key=lambda r: (_series_key(r), r["commit_order"]))
    return rows


def build_latest(points: list[dict]) -> list[dict]:
    """One summary row per series that has at least one measured point."""
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for pt in points:
        if is_measured(pt):
            grouped[_series_key(pt)].append(pt)

    rows: list[dict] = []
    for key in sorted(grouped):
        pts_desc = sorted(grouped[key], key=lambda p: p["commit_order"], reverse=True)
        newest = pts_desc[0]
        hist = [p["estimate"] for p in pts_desc]
        recent = hist[:RECENT]
        diffs = [abs(recent[i] / recent[i + 1] - 1.0) for i in range(len(recent) - 1) if recent[i + 1] != 0]
        noise = float(statistics.median(diffs)) if len(diffs) >= 2 else None
        if len(recent) > 1:
            avg = sum(recent) / len(recent)
            cv = float(statistics.pstdev(recent) / avg) if avg != 0 else None
        else:
            cv = None

        rows.append(
            {
                "suite": newest["suite"],
                "case": newest["case"],
                "params": newest["params"],
                "quantity": newest["quantity"],
                "unit": newest["unit"],
                "direction": newest["direction"],
                "node": newest["node"],
                "sha": newest["sha"],
                "commit_order": newest["commit_order"],
                "committer_date": newest["committer_date"],
                "points": len(pts_desc),
                "first_order": pts_desc[-1]["commit_order"],
                "estimate": hist[0],
                "vs_prev": (hist[0] / recent[1] - 1.0) if len(recent) > 1 and recent[1] != 0 else None,
                "vs_10": (hist[0] / recent[10] - 1.0) if len(recent) > 10 and recent[10] != 0 else None,
                "vs_100": (hist[0] / hist[100] - 1.0) if len(hist) > 100 and hist[100] != 0 else None,
                "noise": noise,
                "cv": cv,
                "noise_rank": 0.0,
            }
        )

    # Standard competition rank (SQL rank() OVER (PARTITION BY suite ORDER BY noise ASC NULLS LAST)).
    by_suite: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_suite[r["suite"]].append(r)
    for suite_rows in by_suite.values():
        denom = max(len(suite_rows) - 1, 1)
        ordered = sorted(suite_rows, key=lambda r: (r["noise"] is None, r["noise"] if r["noise"] is not None else 0.0))
        prev_val = object()
        rank = 0
        for idx, row in enumerate(ordered, start=1):
            val = row["noise"]
            if val != prev_val:
                rank = idx
                prev_val = val
            row["noise_rank"] = (rank - 1) / denom

    rows.sort(key=_series_key)
    return rows


def build_movers(changes: list[dict]) -> list[dict]:
    """Top MOVERS_PER_COMMIT changes by |rel_change| per (sha, node)."""
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for ch in changes:
        if ch["rel_change"] is not None:
            by_pair[(ch["sha"], ch["node"])].append(ch)
    rows: list[dict] = []
    for group in by_pair.values():
        group.sort(key=lambda ch: abs(ch["rel_change"]), reverse=True)
        rows.extend(group[:MOVERS_PER_COMMIT])
    rows.sort(key=lambda ch: (-ch["commit_order"], -abs(ch["rel_change"])))
    return rows


def build_marts(
    runs: list[dict],
    raw_points: list[dict],
    *,
    ingest_problems: int = 0,
    source: str = "store",
    has_parquet_marts: bool = False,
) -> dict:
    """Compute all derived tables and manifest metadata in memory."""
    # Ensure every (sha, node) present in raw_points has a run row so orphan results
    # (for example results.parquet without run.parquet) get a real commit_order and page.
    known_pairs = {(r["sha"], r["node"]) for r in runs}
    orphan_points = [pt for pt in raw_points if (pt["sha"], pt["node"]) not in known_pairs]
    all_runs = list(runs)
    if orphan_points:
        all_runs.extend(synthesize_runs(orphan_points, default_state="unknown"))

    commits = build_commits(all_runs)
    points = build_points(raw_points, commits) if raw_points else []
    changes = build_changes(points) if points else []
    latest = build_latest(points) if points else []
    movers = build_movers(changes) if changes else []

    suite_counts = dict(sorted(Counter(p["suite"] for p in points).items()))
    newest = commits[0] if commits else None
    latest_commit = None
    if newest is not None:
        latest_commit = {
            k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in newest.items() if k != "has_log"
        }

    counts = {
        "commits": len(commits),
        "points": len(points),
        "changes": len(changes),
        "latest": len(latest),
        "movers": len(movers),
        "distinct_commits": len({c["sha"] for c in commits}),
        "nodes": sorted({c["node"] for c in commits}),
        "series": len(latest),
        "ingest_problems": int(ingest_problems),
    }
    tables = (
        {
            "commits": "commits.parquet",
            "points": "points.parquet",
            "changes": "changes.parquet",
            "latest": "latest.parquet",
            "movers": "movers.parquet",
        }
        if has_parquet_marts
        else {}
    )
    manifest = {
        "built_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "counts": counts,
        "latest_commit": latest_commit,
        "suites": suite_counts,
        "tables": tables,
        "notes": {
            "commit_order": "rank of committer_date (breaking ties by ancestry then sha) among measured commits",
            "rel_change": "estimate / previous measured point of the same series - 1; a heuristic, not a verdict",
            "noise": f"median of |consecutive rel_change| over the last {RECENT} points of the series; NULL below 3 points",
        },
    }
    return {
        "commits": commits,
        "points": points,
        "changes": changes,
        "latest": latest,
        "movers": movers,
        "manifest": manifest,
    }


def _mart_schemas():
    pa, _ = _require_pyarrow("writing Parquet marts")
    ts = pa.timestamp("us", tz="UTC")
    return {
        "commits": pa.schema(
            [
                ("sha", pa.string()),
                ("short", pa.string()),
                ("parents", pa.list_(pa.string())),
                ("parent", pa.string()),
                ("author_date", ts),
                ("committer_date", ts),
                ("subject", pa.string()),
                ("branch", pa.string()),
                ("node", pa.string()),
                ("state", pa.string()),
                ("reason", pa.string()),
                ("started_at", ts),
                ("finished_at", ts),
                ("suites_ok", pa.int32()),
                ("suites_failed", pa.int32()),
                ("results", pa.int32()),
                ("commit_order", pa.int32()),
            ]
        ),
        "points": pa.schema(
            [
                ("sha", pa.string()),
                ("commit_order", pa.int32()),
                ("committer_date", ts),
                ("parent", pa.string()),
                ("node", pa.string()),
                ("suite", pa.string()),
                ("case", pa.string()),
                ("params", pa.string()),
                ("quantity", pa.string()),
                ("unit", pa.string()),
                ("direction", pa.string()),
                ("status", pa.string()),
                ("reason", pa.string()),
                ("estimate", pa.float64()),
                ("mean", pa.float64()),
                ("min", pa.float64()),
                ("max", pa.float64()),
                ("n", pa.int32()),
                ("started_at", ts),
                ("run_key", pa.string()),
                ("ingest_key", pa.string()),
            ]
        ),
        "changes": pa.schema(
            [
                ("sha", pa.string()),
                ("commit_order", pa.int32()),
                ("committer_date", ts),
                ("parent", pa.string()),
                ("node", pa.string()),
                ("suite", pa.string()),
                ("case", pa.string()),
                ("params", pa.string()),
                ("quantity", pa.string()),
                ("unit", pa.string()),
                ("direction", pa.string()),
                ("estimate", pa.float64()),
                ("prev_estimate", pa.float64()),
                ("prev_sha", pa.string()),
                ("prev_order", pa.int32()),
                ("rel_change", pa.float64()),
                ("prev_is_parent", pa.bool_()),
                ("gap", pa.int32()),
            ]
        ),
        "latest": pa.schema(
            [
                ("suite", pa.string()),
                ("case", pa.string()),
                ("params", pa.string()),
                ("quantity", pa.string()),
                ("unit", pa.string()),
                ("direction", pa.string()),
                ("node", pa.string()),
                ("sha", pa.string()),
                ("commit_order", pa.int32()),
                ("committer_date", ts),
                ("points", pa.int32()),
                ("first_order", pa.int32()),
                ("estimate", pa.float64()),
                ("vs_prev", pa.float64()),
                ("vs_10", pa.float64()),
                ("vs_100", pa.float64()),
                ("noise", pa.float64()),
                ("cv", pa.float64()),
                ("noise_rank", pa.float64()),
            ]
        ),
    }


def write_parquet_marts(marts: dict, data_dir: Path) -> None:
    """Write the five derived Parquet marts under `data_dir` with small row groups."""
    pa, pq = _require_pyarrow("writing Parquet marts")
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    schemas = _mart_schemas()
    schemas["movers"] = schemas["changes"]

    for name in ("commits", "points", "changes", "latest", "movers"):
        rows = marts[name]
        if name == "commits":
            rows = [{k: v for k, v in r.items() if k != "has_log"} for r in rows]
        table = (
            pa.Table.from_pylist(rows, schema=schemas[name]) if rows else pa.Table.from_pylist([], schema=schemas[name])
        )
        pq.write_table(
            table,
            data_dir / f"{name}.parquet",
            compression="zstd",
            row_group_size=2048,
            write_statistics=True,
        )
