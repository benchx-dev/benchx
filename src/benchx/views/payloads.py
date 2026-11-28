"""Derive the static dashboard's JSON view payloads from the in-memory marts.

No query engine is required; every number shown by the page is computed here in
pure Python and written as small JSON documents under `data_dir / "views"`:

    landing.json          stats, each node's newest commit run (movers & failures),
                          most recently finished run, recent commits, noisiest series,
                          suite wall time
    series.json           one entry per series (including never-succeeded series)
    sparks.json           every series' last SPARK_POINTS points + shared commit axis
    history/<suite>.json  every series of one suite as aligned parallel arrays
    commits/<sha>.json    one commit: its nodes' status, failures, and changes
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from .inputs import UTC, ViewError, _require_pyarrow, safe_path_segment, short_sha
from .marts import is_failed_status, is_measured

VIEWS_FORMATS = ("json", "parquet")
PARQUET_VIEW_FILES = (
    "series.parquet",
    "commits.parquet",
    "commit_nodes.parquet",
    "changes.parquet",
    "failures.parquet",
)
PARQUET_VIEW_ROW_GROUP_SIZE = 10_000

NOTABLE = 0.05
RECENT_COMMITS = 30
MOVERS = 15
NOISIEST = 10
SPARK_POINTS = 60


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    return str(value)


def _safe_child(parent: Path, stem: str, suffix: str = ".json") -> Path:
    base = parent.resolve()
    target = (base / f"{safe_path_segment(stem)}{suffix}").resolve()
    if not target.is_relative_to(base):
        raise ViewError(f"refusing to write outside {base}: {stem!r}")
    return target


def _dump(doc, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")


def signed(direction: str, rel_change) -> str | None:
    """'regressed' or 'improved' for a change, by the quantity's direction."""
    if rel_change is None:
        return None
    worse = rel_change > 0 if direction != "higher-is-better" else rel_change < 0
    return "regressed" if worse else "improved"


def series_key(r: dict) -> tuple:
    return (r["suite"], r["case"], r["params"], r["quantity"], r["node"])


def newest_commits(commits: list[dict]) -> dict[str, dict]:
    """Per node, its run of the newest commit (commit order, not execution time)."""
    out: dict[str, dict] = {}
    for c in commits:  # already sorted by commit_order descending
        out.setdefault(c["node"], c)
    return out


def last_executed(commits: list[dict]) -> dict[str, dict]:
    """Per node, the run that finished (or started) most recently."""
    out: dict[str, dict] = {}
    for c in commits:
        when = c.get("finished_at") or c.get("started_at") or ""
        prev = out.get(c["node"])
        prev_when = (prev.get("finished_at") or prev.get("started_at") or "") if prev else ""
        if prev is None or when > prev_when:
            out[c["node"]] = c
    return out


def _serialize_commit(c: dict, notable_entry: dict, failed_count: int) -> dict:
    return {
        "sha": c["sha"],
        "short": c["short"],
        "parent": c["parent"],
        "subject": c["subject"],
        "committer_date": _iso(c["committer_date"]),
        "node": c["node"],
        "state": c["state"],
        "reason": c["reason"],
        "started_at": _iso(c["started_at"]),
        "finished_at": _iso(c["finished_at"]),
        "suites_ok": c["suites_ok"],
        "suites_failed": c["suites_failed"],
        "results": c["results"],
        "commit_order": c["commit_order"],
        "has_log": bool(c.get("has_log", False)),
        "regressed": notable_entry.get("regressed", 0),
        "improved": notable_entry.get("improved", 0),
        "compared": notable_entry.get("compared", 0),
        "failed_results": failed_count,
    }


def _write_commit_pages(
    out: Path,
    commits: list[dict],
    changes_by_sha: dict[str, list[dict]],
    failures_by_sha: dict[str, list[dict]],
) -> int:
    commits_dir = out / "commits"
    if commits_dir.is_dir():
        for stale in commits_dir.glob("*.json"):
            stale.unlink()
    by_sha: dict[str, list[dict]] = {}
    for c in commits:
        by_sha.setdefault(c["sha"], []).append(c)
    for sha, nodes_of in by_sha.items():
        head = nodes_of[0]
        _dump(
            {
                "sha": sha,
                "short": head["short"],
                "subject": head["subject"],
                "committer_date": head["committer_date"],
                "parent": head["parent"],
                "commit_order": head["commit_order"],
                "nodes": nodes_of,
                "failures": failures_by_sha.get(sha, []),
                "changes": changes_by_sha.get(sha, []),
            },
            _safe_child(commits_dir, sha),
        )
    return len(by_sha)


def _write_pq(rows: list[dict], schema, path: Path, *, row_group_size: int | None = None) -> None:
    pa, pq = _require_pyarrow("writing Parquet views")
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    kwargs: dict = {"compression": "snappy"}
    if row_group_size is not None:
        kwargs["row_group_size"] = row_group_size
    pq.write_table(table, path, **kwargs)


def _parquet_view_schemas():
    pa, _ = _require_pyarrow("writing Parquet views")
    i32, f64, s, b = pa.int32(), pa.float64(), pa.string(), pa.bool_()
    return {
        "series": pa.schema(
            [
                ("suite", s),
                ("case", s),
                ("params", s),
                ("quantity", s),
                ("unit", s),
                ("direction", s),
                ("node", s),
                ("sha", s),
                ("commit_order", i32),
                ("first_order", i32),
                ("committer_date", s),
                ("points", i32),
                ("estimate", f64),
                ("vs_prev", f64),
                ("vs_10", f64),
                ("vs_100", f64),
                ("noise", f64),
                ("cv", f64),
                ("noise_rank", f64),
                ("status", s),
                ("reason", s),
                ("attempt_order", i32),
                ("attempt_sha", s),
            ]
        ),
        "history": pa.schema(
            [
                ("case", s),
                ("params", s),
                ("quantity", s),
                ("unit", s),
                ("direction", s),
                ("node", s),
                ("commit_order", i32),
                ("estimate", f64),
                ("min", f64),
                ("max", f64),
                ("n", i32),
                ("status", s),
                ("reason", s),
            ]
        ),
        "commits": pa.schema(
            [
                ("sha", s),
                ("short", s),
                ("subject", s),
                ("committer_date", s),
                ("parent", s),
                ("commit_order", i32),
                ("changes_start", i32),
                ("changes_end", i32),
                ("failures_start", i32),
                ("failures_end", i32),
            ]
        ),
        "commit_nodes": pa.schema(
            [
                ("sha", s),
                ("short", s),
                ("parent", s),
                ("subject", s),
                ("committer_date", s),
                ("node", s),
                ("state", s),
                ("reason", s),
                ("started_at", s),
                ("finished_at", s),
                ("suites_ok", i32),
                ("suites_failed", i32),
                ("results", i32),
                ("commit_order", i32),
                ("has_log", b),
                ("regressed", i32),
                ("improved", i32),
                ("compared", i32),
                ("failed_results", i32),
            ]
        ),
        "changes": pa.schema(
            [
                ("sha", s),
                ("suite", s),
                ("case", s),
                ("params", s),
                ("quantity", s),
                ("unit", s),
                ("direction", s),
                ("node", s),
                ("estimate", f64),
                ("prev_estimate", f64),
                ("rel_change", f64),
                ("prev_sha", s),
                ("prev_is_parent", b),
                ("gap", i32),
                ("noise", f64),
            ]
        ),
        "failures": pa.schema(
            [
                ("sha", s),
                ("suite", s),
                ("case", s),
                ("params", s),
                ("quantity", s),
                ("node", s),
                ("status", s),
                ("reason", s),
            ]
        ),
    }


def _clean_stale_views(out: Path, views_format: str) -> None:
    history_dir = out / "history"
    commits_dir = out / "commits"
    if history_dir.is_dir():
        for stale in list(history_dir.glob("*.json")) + list(history_dir.glob("*.parquet")):
            stale.unlink()
    if commits_dir.is_dir():
        for stale in commits_dir.glob("*.json"):
            stale.unlink()
    if views_format == "parquet":
        for name in ("series.json", "sparks.json"):
            p = out / name
            if p.exists():
                p.unlink()
    else:
        for name in PARQUET_VIEW_FILES:
            p = out / name
            if p.exists():
                p.unlink()


def _write_commit_parquet(
    out: Path,
    commits: list[dict],
    changes_by_sha: dict[str, list[dict]],
    failures_by_sha: dict[str, list[dict]],
    *,
    format_change=None,
) -> int:
    pa, pq = _require_pyarrow("writing Parquet views")
    schemas = _parquet_view_schemas()
    by_sha: dict[str, list[dict]] = {}
    for c in commits:
        by_sha.setdefault(c["sha"], []).append(c)

    out.mkdir(parents=True, exist_ok=True)
    commit_rows: list[dict] = []
    c_total = 0
    f_total = 0
    c_buf: list[dict] = []
    f_buf: list[dict] = []
    chunk = PARQUET_VIEW_ROW_GROUP_SIZE

    def _flush(writer, buf: list[dict], schema) -> None:
        if buf:
            writer.write_table(pa.Table.from_pylist(buf, schema=schema))
            buf.clear()

    with (
        pq.ParquetWriter(out / "changes.parquet", schemas["changes"], compression="snappy") as c_writer,
        pq.ParquetWriter(out / "failures.parquet", schemas["failures"], compression="snappy") as f_writer,
    ):
        for sha, nodes_of in by_sha.items():
            head = nodes_of[0]
            sha_changes = changes_by_sha.pop(sha, ())
            c_start = c_total
            for ch in sha_changes:
                row = format_change(ch) if format_change is not None else ch
                c_buf.append({"sha": sha, **row})
                if len(c_buf) >= chunk:
                    _flush(c_writer, c_buf, schemas["changes"])
            c_total += len(sha_changes)
            c_end = c_total

            sha_failures = failures_by_sha.pop(sha, ())
            f_start = f_total
            for f in sha_failures:
                f_buf.append({"sha": sha, **f})
                if len(f_buf) >= chunk:
                    _flush(f_writer, f_buf, schemas["failures"])
            f_total += len(sha_failures)
            f_end = f_total

            commit_rows.append(
                {
                    "sha": sha,
                    "short": head["short"],
                    "subject": head["subject"],
                    "committer_date": head["committer_date"],
                    "parent": head["parent"],
                    "commit_order": head["commit_order"],
                    "changes_start": c_start,
                    "changes_end": c_end,
                    "failures_start": f_start,
                    "failures_end": f_end,
                }
            )

        _flush(c_writer, c_buf, schemas["changes"])
        _flush(f_writer, f_buf, schemas["failures"])

    _write_pq(commit_rows, schemas["commits"], out / "commits.parquet")
    _write_pq(commits, schemas["commit_nodes"], out / "commit_nodes.parquet")
    return len(by_sha)


def _build_empty(out: Path, commits: list[dict], *, views_format: str = "json") -> dict:
    _clean_stale_views(out, views_format)
    newest_by_node = newest_commits(commits)
    if views_format == "parquet":
        schemas = _parquet_view_schemas()
        _write_pq([], schemas["series"], out / "series.parquet")
        commit_pages = _write_commit_parquet(out, commits, {}, {})
    else:
        _dump([], out / "series.json")
        _dump({"axis": [], "series": []}, out / "sparks.json")
        commit_pages = _write_commit_pages(out, commits, {}, {})
    _dump(
        {
            "built_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
            "notable": NOTABLE,
            "stats": {
                "commits": len({c["sha"] for c in commits}),
                "results": 0,
                "series": 0,
                "nodes": sorted(newest_by_node),
                "latest": commits[0] if commits else None,
            },
            "latest_runs": list(newest_by_node.values()),
            "last_executed": last_executed(commits),
            "recent_commits": commits[:RECENT_COMMITS],
            "movers": {"regressed": [], "improved": []},
            "failures": [],
            "noisiest": [],
            "suite_time": [],
        },
        out / "landing.json",
    )
    return {
        "series": 0,
        "recent_commits": min(len(commits), RECENT_COMMITS),
        "sparks": 0,
        "history_suites": 0,
        "commit_pages": commit_pages,
    }


def build_views(marts: dict, data_dir: Path, *, views_format: str = "json") -> dict:
    """Write every JSON or Parquet view payload under `data_dir / 'views'` and return counts."""
    if views_format not in VIEWS_FORMATS:
        raise ViewError(f"unsupported views_format {views_format!r}; expected one of {', '.join(VIEWS_FORMATS)}")
    if views_format == "parquet":
        _require_pyarrow("writing Parquet views")
    out = Path(data_dir).resolve() / "views"
    commits_raw = marts["commits"]
    points = marts["points"]
    changes = marts["changes"]
    latest_raw = marts["latest"]
    has_points = len(points) > 0

    notable: dict[tuple[str, str], dict] = {}
    failed_results: dict[tuple[str, str], int] = defaultdict(int)
    if has_points:
        for ch in changes:
            pair = (ch["sha"], ch["node"])
            entry = notable.setdefault(pair, {"regressed": 0, "improved": 0, "compared": 0})
            entry["compared"] += 1
            rel = ch["rel_change"]
            if rel is not None and abs(rel) > NOTABLE:
                kind = signed(ch["direction"], rel)
                if kind == "regressed":
                    entry["regressed"] += 1
                elif kind == "improved":
                    entry["improved"] += 1
        for pt in points:
            if is_failed_status(pt["status"]):
                failed_results[(pt["sha"], pt["node"])] += 1

    commits = [
        _serialize_commit(
            c,
            notable.get((c["sha"], c["node"]), {}),
            failed_results.get((c["sha"], c["node"]), 0),
        )
        for c in commits_raw
    ]
    if not has_points:
        return _build_empty(out, commits, views_format=views_format)

    _clean_stale_views(out, views_format)
    counts: dict[str, int] = {}

    # Latest attempt of every series (measured, skipped, or failed).
    attempts: dict[tuple, dict] = {}
    for pt in points:
        key = series_key(pt)
        prev = attempts.get(key)
        if prev is None or pt["commit_order"] >= prev["attempt_order"]:
            attempts[key] = {
                "suite": pt["suite"],
                "case": pt["case"],
                "params": pt["params"],
                "quantity": pt["quantity"],
                "node": pt["node"],
                "unit": pt["unit"],
                "direction": pt["direction"],
                "status": pt["status"],
                "reason": pt["reason"],
                "attempt_order": pt["commit_order"],
                "attempt_sha": pt["sha"],
            }

    latest_by_key: dict[tuple, dict] = {}
    series_list: list[dict] = []
    for r in latest_raw:
        key = series_key(r)
        a = attempts[key]
        entry = {
            "suite": r["suite"],
            "case": r["case"],
            "params": r["params"],
            "quantity": r["quantity"],
            "unit": r["unit"],
            "direction": r["direction"],
            "node": r["node"],
            "sha": r["sha"],
            "commit_order": r["commit_order"],
            "first_order": r["first_order"],
            "committer_date": _iso(r["committer_date"]),
            "points": r["points"],
            "estimate": r["estimate"],
            "vs_prev": r["vs_prev"],
            "vs_10": r["vs_10"],
            "vs_100": r["vs_100"],
            "noise": r["noise"],
            "cv": r["cv"],
            "noise_rank": r["noise_rank"],
            "status": a["status"],
            "reason": a["reason"],
            "attempt_order": a["attempt_order"],
            "attempt_sha": a["attempt_sha"],
        }
        latest_by_key[key] = entry
        series_list.append(entry)

    for key, a in attempts.items():
        if key not in latest_by_key:
            entry = {
                **a,
                "sha": a["attempt_sha"],
                "commit_order": a["attempt_order"],
                "first_order": None,
                "committer_date": None,
                "points": 0,
                "estimate": None,
                "vs_prev": None,
                "vs_10": None,
                "vs_100": None,
                "noise": None,
                "cv": None,
                "noise_rank": None,
            }
            latest_by_key[key] = entry
            series_list.append(entry)

    series_list.sort(key=series_key)
    counts["series"] = len(series_list)
    if views_format == "parquet":
        schemas = _parquet_view_schemas()
        _write_pq(series_list, schemas["series"], out / "series.parquet")
    else:
        _dump(series_list, out / "series.json")

    # Movers on each node's latest commit with changes.
    top_change_order: dict[str, int] = {}
    for ch in changes:
        node = ch["node"]
        if node not in top_change_order or ch["commit_order"] > top_change_order[node]:
            top_change_order[node] = ch["commit_order"]

    top_movers: list[dict] = []
    for ch in changes:
        if ch["rel_change"] is None or ch["commit_order"] != top_change_order.get(ch["node"]):
            continue
        lt = latest_by_key.get(series_key(ch))
        top_movers.append(
            {
                "suite": ch["suite"],
                "case": ch["case"],
                "params": ch["params"],
                "quantity": ch["quantity"],
                "unit": ch["unit"],
                "direction": ch["direction"],
                "node": ch["node"],
                "sha": ch["sha"],
                "short": short_sha(ch["sha"]),
                "estimate": ch["estimate"],
                "prev_estimate": ch["prev_estimate"],
                "rel_change": ch["rel_change"],
                "prev_sha": ch["prev_sha"],
                "prev_is_parent": ch["prev_is_parent"],
                "gap": ch["gap"],
                "noise": lt["noise"] if lt else None,
                "points": lt["points"] if lt else None,
            }
        )
    top_movers.sort(key=lambda m: abs(m["rel_change"]), reverse=True)

    regressed: list[dict] = []
    improved: list[dict] = []
    per_node: dict[tuple[str, str | None], int] = defaultdict(int)
    for m in top_movers:
        kind = signed(m["direction"], m["rel_change"])
        per_node[(m["node"], kind)] += 1
        if per_node[(m["node"], kind)] <= MOVERS:
            (regressed if kind == "regressed" else improved).append(m)

    # Failures on each node's newest commit.
    newest_by_node = newest_commits(commits)
    failures: list[dict] = []
    for key in sorted(attempts):
        a = attempts[key]
        top = newest_by_node.get(key[4])
        if top and a["attempt_sha"] == top["sha"] and is_failed_status(a["status"]):
            last_ok = latest_by_key[key]
            failures.append(
                {
                    "suite": key[0],
                    "case": key[1],
                    "params": key[2],
                    "quantity": key[3],
                    "node": key[4],
                    "status": a["status"],
                    "reason": a["reason"],
                    "sha": a["attempt_sha"],
                    "last_success_sha": last_ok["sha"] if last_ok.get("points") else None,
                }
            )

    noisiest = sorted(
        (
            {
                "suite": r["suite"],
                "case": r["case"],
                "params": r["params"],
                "quantity": r["quantity"],
                "node": r["node"],
                "noise": r["noise"],
                "cv": r["cv"],
                "points": r["points"],
                "noise_rank": r["noise_rank"],
            }
            for r in latest_raw
            if r["noise"] is not None and r["points"] >= 3
        ),
        key=lambda r: r["noise"],
        reverse=True,
    )[:NOISIEST]

    # Suite wall time on each node's latest commit with points.
    top_point_order: dict[str, int] = {}
    for pt in points:
        node = pt["node"]
        if node not in top_point_order or pt["commit_order"] > top_point_order[node]:
            top_point_order[node] = pt["commit_order"]

    suite_groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for pt in points:
        if pt["commit_order"] == top_point_order.get(pt["node"]):
            suite_groups[(pt["suite"], pt["node"], pt["sha"])].append(pt)
    suite_time = []
    for (suite, node, sha), pts in suite_groups.items():
        times = [p["started_at"] for p in pts if p["started_at"] is not None]
        seconds = int((max(times) - min(times)).total_seconds()) if len(times) >= 2 else 0
        suite_time.append(
            {
                "suite": suite,
                "node": node,
                "sha": sha,
                "short": short_sha(sha),
                "cases": len(pts),
                "failed": sum(1 for p in pts if is_failed_status(p["status"])),
                "seconds": seconds,
            }
        )
    suite_time.sort(key=lambda r: (-r["seconds"], r["suite"], r["node"]))

    nodes = sorted({c["node"] for c in commits})
    landing = {
        "built_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        "notable": NOTABLE,
        "stats": {
            "commits": len({p["sha"] for p in points}),
            "results": len(points),
            "series": len(series_list),
            "nodes": nodes,
            "latest": commits[0] if commits else None,
        },
        "latest_runs": list(newest_by_node.values()),
        "last_executed": last_executed(commits),
        "recent_commits": commits[:RECENT_COMMITS],
        "movers": {"regressed": regressed, "improved": improved},
        "failures": failures,
        "noisiest": noisiest,
        "suite_time": suite_time,
    }
    _dump(landing, out / "landing.json")
    counts["recent_commits"] = len(landing["recent_commits"])

    if views_format == "json":
        # Sparks: last SPARK_POINTS commits and per-series measured points.
        axis_by_order: dict[int, dict] = {}
        for pt in points:
            order = pt["commit_order"]
            if order not in axis_by_order:
                axis_by_order[order] = {
                    "commit_order": order,
                    "sha": pt["sha"],
                    "short": short_sha(pt["sha"]),
                    "date": _iso(pt["committer_date"]),
                }
        spark_axis = [axis_by_order[o] for o in sorted(axis_by_order)[-SPARK_POINTS:]]

        ok_by_series: dict[tuple, list[dict]] = defaultdict(list)
        for pt in points:
            if is_measured(pt):
                ok_by_series[series_key(pt)].append(pt)

        spark_series = []
        for key in sorted(ok_by_series):
            pts = sorted(ok_by_series[key], key=lambda p: p["commit_order"])[-SPARK_POINTS:]
            lt = latest_by_key.get(key, {})
            a = attempts[key]
            head = pts[-1]
            spark_series.append(
                {
                    "suite": head["suite"],
                    "case": head["case"],
                    "params": head["params"],
                    "quantity": head["quantity"],
                    "unit": head["unit"],
                    "direction": head["direction"],
                    "node": head["node"],
                    "latest": lt.get("estimate"),
                    "vs_prev": lt.get("vs_prev"),
                    "noise": lt.get("noise"),
                    "points": lt.get("points"),
                    "sha": lt.get("sha"),
                    "orders": [p["commit_order"] for p in pts],
                    "estimates": [p["estimate"] for p in pts],
                    "status": a["status"],
                    "reason": a["reason"],
                }
            )
        _dump({"axis": spark_axis, "series": spark_series}, out / "sparks.json")
        counts["sparks"] = len(spark_series)
    else:
        counts["sparks"] = 0

    # History per suite.
    history_dir = out / "history"
    subject_by_pair = {(c["sha"], c["node"]): c["subject"] for c in commits}
    points_by_suite: dict[str, list[dict]] = defaultdict(list)
    for pt in points:
        points_by_suite[pt["suite"]].append(pt)
    counts["history_suites"] = len(points_by_suite)

    if views_format == "parquet":
        schemas = _parquet_view_schemas()
        for suite in sorted(points_by_suite):
            suite_pts = points_by_suite.pop(suite)
            suite_pts.sort(
                key=lambda p: (p["case"], p["params"], p["quantity"], p["node"], p["commit_order"]),
            )
            hist_rows = [
                {
                    "case": p["case"],
                    "params": p["params"],
                    "quantity": p["quantity"],
                    "unit": p["unit"],
                    "direction": p["direction"],
                    "node": p["node"],
                    "commit_order": p["commit_order"],
                    "estimate": p["estimate"],
                    "min": p["min"],
                    "max": p["max"],
                    "n": p["n"],
                    "status": p["status"],
                    "reason": p["reason"],
                }
                for p in suite_pts
            ]
            _write_pq(hist_rows, schemas["history"], _safe_child(history_dir, suite, suffix=".parquet"))
            del suite_pts, hist_rows
    else:
        for suite in sorted(points_by_suite):
            suite_pts = points_by_suite.pop(suite)
            axis_map: dict[tuple[int, str], dict] = {}
            series_groups: dict[tuple, list[dict]] = defaultdict(list)
            for pt in suite_pts:
                axis_key = (pt["commit_order"], pt["sha"])
                if axis_key not in axis_map:
                    axis_map[axis_key] = {
                        "commit_order": pt["commit_order"],
                        "sha": pt["sha"],
                        "short": short_sha(pt["sha"]),
                        "date": _iso(pt["committer_date"]),
                        "subject": subject_by_pair.get((pt["sha"], pt["node"]), ""),
                    }
                series_groups[(pt["case"], pt["params"], pt["quantity"], pt["node"])].append(pt)

            axis = [axis_map[k] for k in sorted(axis_map)]
            series_map: dict[str, dict] = {}
            for skey in sorted(series_groups):
                pts = sorted(series_groups[skey], key=lambda p: p["commit_order"])
                head = pts[0]
                series_map["|".join(skey)] = {
                    "case": head["case"],
                    "params": head["params"],
                    "quantity": head["quantity"],
                    "unit": head["unit"],
                    "direction": head["direction"],
                    "node": head["node"],
                    "orders": [p["commit_order"] for p in pts],
                    "estimates": [p["estimate"] for p in pts],
                    "mins": [p["min"] for p in pts],
                    "maxs": [p["max"] for p in pts],
                    "ns": [p["n"] for p in pts],
                    "statuses": [p["status"] for p in pts],
                    "reasons": [p["reason"] for p in pts],
                }
            _dump({"suite": suite, "commits": axis, "series": series_map}, _safe_child(history_dir, suite))

    # Per-commit pages: nodes, failures, and every change (largest first).
    # Group references to `changes` rather than copying every row up front so
    # multi-million-row builds stream to Parquet without doubling peak RAM.
    changes_by_sha: dict[str, list[dict]] = defaultdict(list)
    for ch in changes:
        changes_by_sha[ch["sha"]].append(ch)
    for sha_changes in changes_by_sha.values():
        sha_changes.sort(key=lambda ch: abs(ch["rel_change"]) if ch["rel_change"] is not None else -1.0, reverse=True)

    def _view_change(ch: dict) -> dict:
        lt = latest_by_key.get(series_key(ch))
        return {
            "suite": ch["suite"],
            "case": ch["case"],
            "params": ch["params"],
            "quantity": ch["quantity"],
            "unit": ch["unit"],
            "direction": ch["direction"],
            "node": ch["node"],
            "estimate": ch["estimate"],
            "prev_estimate": ch["prev_estimate"],
            "rel_change": ch["rel_change"],
            "prev_sha": ch["prev_sha"],
            "prev_is_parent": ch["prev_is_parent"],
            "gap": ch["gap"],
            "noise": lt["noise"] if lt else None,
        }

    failures_by_sha: dict[str, list[dict]] = defaultdict(list)
    for pt in points:
        if is_failed_status(pt["status"]):
            failures_by_sha[pt["sha"]].append(
                {
                    "suite": pt["suite"],
                    "case": pt["case"],
                    "params": pt["params"],
                    "quantity": pt["quantity"],
                    "node": pt["node"],
                    "status": pt["status"],
                    "reason": pt["reason"],
                }
            )

    if views_format == "parquet":
        counts["commit_pages"] = _write_commit_parquet(
            out, commits, changes_by_sha, failures_by_sha, format_change=_view_change
        )
    else:
        formatted = {sha: [_view_change(ch) for ch in rows] for sha, rows in changes_by_sha.items()}
        counts["commit_pages"] = _write_commit_pages(out, commits, formatted, failures_by_sha)
    return counts
