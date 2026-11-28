"""Read benchmark results for the views layer from a local store, JSON files, or a CI runs/ tree.

Three inputs, one normalized shape (`runs`, `raw_points`, `ingest_problems`):

  runs/ directory  runs/<sha>/<node>/{run.parquet, results.parquet} (CI / node layout)
  local store      store.parquet (benchx.store.Store)
  JSON results     result files and work orders, read via MemoryStore without PyArrow

PyArrow is imported lazily only when reading or writing Parquet files, so JSON-only
views work with a minimal `pip install benchx`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import rfc8785

from .. import identity, projection
from ..errors import BenchxError
from ..results import MemoryStore

UTC = dt.UTC
_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_PATH_UNSAFE_RE = re.compile(r'[/\\\x00:*?"<>|]+')
_DOTS_RUN_RE = re.compile(r"\.\.+")


class ViewError(BenchxError):
    """The view inputs could not be read or built."""


def check_local_path(value: str | Path) -> Path:
    """Reject remote URIs (such as s3:// or https://) before turning them into Paths."""
    text = str(value)
    if _URI_SCHEME_RE.match(text):
        raise ViewError(
            f"remote URI {text!r} is not supported directly; "
            "sync the files locally first (for example with `aws s3 sync`)"
        )
    return Path(value)


def safe_path_segment(value: str) -> str:
    """Flatten path separators and traversal dots so generated filenames stay inside out/."""
    cleaned = _DOTS_RUN_RE.sub("_", _PATH_UNSAFE_RE.sub("_", str(value)))
    return "_" if cleaned in ("", ".") else cleaned


def short_sha(sha: str) -> str:
    """Short display label for a commit SHA, preserving a visible marker for dirty trees."""
    if "-dirty-" in sha:
        base, tree = sha.split("-dirty-", 1)
        return f"{base[:7]}+dirty-{tree[:4]}"
    return sha[:12]


def _require_pyarrow(feature: str = "Parquet view support"):
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "pyarrow":
            raise ModuleNotFoundError(
                f"{feature} requires pyarrow; install it with `pip install 'benchx[parquet]'`.",
                name="pyarrow",
            ) from exc
        raise
    return pa, pq


def parse_time(value) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp or datetime into a UTC datetime (or None)."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def run_schema():
    """Arrow schema for runs/<sha>/<node>/run.parquet."""
    pa, _ = _require_pyarrow("run.parquet")
    ts = pa.timestamp("us", tz="UTC")
    return pa.schema(
        [
            ("sha", pa.string()),
            ("parents", pa.list_(pa.string())),
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
            ("ingest_problems", pa.int32()),
            ("log", pa.string()),
        ]
    )


def write_run(
    node_dir: Path,
    commit: dict,
    status: dict,
    log: str = "",
    ingest_problems: int = 0,
) -> Path:
    """Write one run's `run.parquet` from commit metadata, status, and log text."""
    pa, pq = _require_pyarrow("write_run")
    suites = status.get("suites") or {}
    node_dir = Path(node_dir)
    node_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "sha": commit["sha"],
        "parents": list(commit.get("parents") or []),
        "author_date": parse_time(commit.get("author_date")),
        "committer_date": parse_time(commit.get("committer_date")),
        "subject": commit.get("subject") or "",
        "branch": commit.get("branch") or "main",
        "node": status.get("node") or node_dir.name,
        "state": status.get("state") or "unknown",
        "reason": status.get("reason"),
        "started_at": parse_time(status.get("started_at")),
        "finished_at": parse_time(status.get("finished_at")),
        "suites_ok": int(status.get("suites_ok") or 0),
        "suites_failed": int(status.get("suites_failed") or 0),
        "results": int(sum((s or {}).get("results", 0) for s in suites.values())),
        "ingest_problems": int(ingest_problems),
        "log": log,
    }
    out = node_dir / "run.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=run_schema()), out, compression="zstd")
    return out


def read_runs_metadata(runs_dir: Path) -> list[dict]:
    """Read run metadata from `runs.parquet` (monthly layout) and `<sha>/<node>/run.parquet`.

    When the same `(sha, node)` appears in both `runs.parquet` and a per-run
    `<sha>/<node>/run.parquet`, the per-run file takes precedence.
    """
    _, pq = _require_pyarrow("reading run.parquet")
    root = Path(runs_dir)
    paths: list[Path] = []
    root_runs = root / "runs.parquet"
    if root_runs.is_file():
        paths.append(root_runs)
    paths.extend(sorted(root.glob("*/*/run.parquet")))
    by_key: dict[tuple[str, str], dict] = {}
    for path in paths:
        table = pq.ParquetFile(path).read()
        for row in table.to_pylist():
            for key in ("author_date", "committer_date", "started_at", "finished_at"):
                row[key] = parse_time(row.get(key))
            row["parents"] = list(row.get("parents") or [])
            row["log"] = row.get("log") or ""
            row["ingest_problems"] = int(row.get("ingest_problems") or 0)
            by_key[(row["sha"], row["node"])] = row
    return list(by_key.values())


def _parse_json_obj(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _canonical_params(value) -> str:
    if value is None or value == "" or value == "{}":
        return ""
    if isinstance(value, str):
        return "" if value == "{}" else value
    if isinstance(value, dict):
        return "" if not value else rfc8785.dumps(value).decode()
    return str(value)


def _normalize_suite_name(raw: str) -> str:
    text = str(raw).replace("\\", "/").strip()
    if not text:
        return "default"
    # Preserve relative Python script paths (e.g. benchmarks/a/bench.py) so nested
    # suites sharing a filename do not collide, while stripping directory prefixes
    # from absolute paths and compiled binary paths (e.g. release/arrow-foo-benchmark).
    if not text.startswith("/") and not (len(text) >= 2 and text[1] == ":") and text.endswith(".py"):
        return text.removeprefix("./") or "default"
    return text.rsplit("/", 1)[-1] or "default"


def _extract_suite(work_order: dict, doc_or_row: dict) -> str:
    suites = work_order.get("suites") if isinstance(work_order, dict) else None
    if isinstance(suites, list) and suites:
        first = suites[0]
        if isinstance(first, dict) and first.get("suite"):
            return _normalize_suite_name(first["suite"])
    provenance = doc_or_row.get("provenance") or {}
    info = _parse_json_obj(provenance.get("info"))
    if info.get("suite"):
        return _normalize_suite_name(info["suite"])
    return "default"


def _extract_node(run_key: str, env_identity: dict) -> str:
    # A CI/node run key has the form <branch>/<sha>/<NODE_NAME>; keep that node name
    # because it is what runs/ is filed under even when a runner renames its hostname.
    if run_key and run_key.count("/") >= 2:
        return run_key.split("/")[-1]
    runner = env_identity.get("runner") or env_identity.get("hostname")
    return str(runner) if runner else "local"


def _extract_estimate(measurement: dict) -> tuple[list[float], float | None, float | None, float | None, float | None]:
    """Extract observation values and (estimate, mean, min, max) following identity.points()."""
    obs = measurement.get("observations") or []
    vals: list[float] = []
    for item in obs:
        if isinstance(item, dict):
            if item.get("included") is False:
                continue
            v = item.get("value")
        else:
            v = item
        if v is not None:
            vals.append(float(v))
    if vals:
        est = projection.estimate(identity.MEDIAN, vals)
        return vals, float(est) if est is not None else None, sum(vals) / len(vals), min(vals), max(vals)

    # Producer-supplied summaries (schema §4.5): {"type": "estimate", "estimator": {"name": ...}, "value": ...}.
    summaries = measurement.get("summaries") or []
    estimates: dict[str, float] = {}
    first_estimate: float | None = None
    stats: dict[str, float] = {}
    if isinstance(summaries, list):
        for s in summaries:
            if not isinstance(s, dict) or s.get("value") is None:
                continue
            val = float(s["value"])
            if s.get("type") == "estimate":
                if first_estimate is None:
                    first_estimate = val
                name = (s.get("estimator") or {}).get("name")
                if name and name not in estimates:
                    estimates[str(name)] = val
            elif s.get("type") == "statistic" and s.get("name"):
                stats.setdefault(str(s["name"]), val)

    est = estimates.get("median", estimates.get("mean", first_estimate))
    if est is None:
        return [], None, None, None, None
    mean = estimates.get("mean", stats.get("mean", est))
    lo = stats.get("min", est)
    hi = stats.get("max", est)
    return [], est, mean, lo, hi


def _point_from_row(row: dict, *, distinguish_dirty: bool, intern=None) -> dict | None:
    measurement = row.get("measurement") or {}
    kind = measurement.get("kind")
    if kind is not None and kind != "measurand":
        return None

    revision = row.get("revision") or {}
    base_sha = str(revision.get("key") or "")
    provenance = row.get("provenance") or {}
    subject_dirty = provenance.get("subject_dirty")
    subject_tree = provenance.get("subject_tree")
    if distinguish_dirty and subject_dirty == "dirty" and subject_tree:
        sha = f"{base_sha[:12]}-dirty-{str(subject_tree)[:8]}"
        dirty_parent = base_sha
    else:
        sha = base_sha
        dirty_parent = None

    run_key = str(provenance.get("run_key") or "")
    coordinates = row.get("coordinates") or {}
    workload = coordinates.get("workload") or {}
    env = coordinates.get("environment") or {}
    env_identity = _parse_json_obj(env.get("identity"))
    work_order = _parse_json_obj(row.get("work_order"))

    quantity = measurement.get("quantity") or {}
    vals, estimate, mean, lo, hi = _extract_estimate(measurement)

    s = intern if intern is not None else (lambda v: v)
    return {
        "sha": s(sha),
        "node": s(_extract_node(run_key, env_identity)),
        "suite": s(_extract_suite(work_order, row)),
        "case": s(str(workload.get("name") or "")),
        "params": s(_canonical_params(workload.get("parameters"))),
        "quantity": s(str(quantity.get("name") or "")),
        "unit": s(str(quantity.get("unit") or "")),
        "direction": s(str(quantity.get("direction") or "")),
        "status": s(str(measurement.get("status") or "unknown")),
        "reason": s(str(measurement.get("reason") or "")),
        "estimate": estimate,
        "mean": mean,
        "min": lo,
        "max": hi,
        "n": len(vals),
        "started_at": parse_time(provenance.get("started_at")),
        "run_key": s(run_key),
        "ingest_key": str(row.get("ingest_key") or ""),
        "_vals": vals,
        "_dirty_parent": dirty_parent,
    }


_PARQUET_POINT_COLUMNS = [
    "revision",
    "coordinates",
    "measurement",
    "provenance",
    "ingest_key",
    "work_order",
]


def _read_parquet_points(path: Path, *, distinguish_dirty: bool, intern=None) -> list[dict]:
    _, pq = _require_pyarrow("reading Parquet results")
    pf = pq.ParquetFile(path)
    available = set(pf.schema_arrow.names)
    cols = [c for c in _PARQUET_POINT_COLUMNS if c in available]
    if not cols:
        return []
    if intern is None:
        cache: dict[str, str] = {}
        intern = lambda v: cache.setdefault(v, v)  # noqa: E731
    points = []
    for batch in pf.iter_batches(batch_size=100_000, columns=cols):
        for row in batch.to_pylist():
            pt = _point_from_row(row, distinguish_dirty=distinguish_dirty, intern=intern)
            if pt is not None:
                points.append(pt)
    return points


def _collapse_duplicate_points(points: list[dict]) -> list[dict]:
    """Combine multiple results for the same (sha, node, suite, case, params, quantity).

    In CI runs/ each commit has one result per series so this is a no-op. In a local
    store populated by `bx compare --rounds R`, combining rounds yields one point per
    commit with pooled observations instead of duplicate commit_order entries.
    """
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for pt in points:
        key = (pt["sha"], pt["node"], pt["suite"], pt["case"], pt["params"], pt["quantity"])
        grouped[key].append(pt)

    out: list[dict] = []
    for group in grouped.values():
        if len(group) == 1:
            pt = dict(group[0])
            pt.pop("_vals", None)
            pt.pop("_dirty_parent", None)
            out.append(pt)
            continue

        group.sort(key=lambda p: (p["started_at"] or dt.datetime.min.replace(tzinfo=UTC), p["ingest_key"]))
        measured = [p for p in group if p["status"] in ("success", "partial") and p["estimate"] is not None]
        if measured:
            vals: list[float] = []
            for p in measured:
                vals.extend(p.get("_vals") or ([p["estimate"]] if p["estimate"] is not None else []))
            latest = measured[-1]
            merged = dict(latest)
            est = projection.estimate(identity.MEDIAN, vals)
            merged["estimate"] = float(est) if est is not None else None
            merged["mean"] = sum(vals) / len(vals)
            merged["min"] = min(vals)
            merged["max"] = max(vals)
            merged["n"] = sum(p["n"] for p in measured)
        else:
            merged = dict(group[-1])
        merged.pop("_vals", None)
        merged.pop("_dirty_parent", None)
        out.append(merged)
    return out


def _git_commit_info(shas: list[str], repo: Path) -> dict[str, dict]:
    """Batch-read commit parents, timestamps, and subject from a local git repo."""
    candidates = sorted({s for s in shas if _HEX_RE.match(s)})
    if not candidates or not repo.exists():
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input="".join(f"{sha}\n" for sha in candidates).encode("utf-8"),
            capture_output=True,
            check=False,
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}

    data = proc.stdout
    pos = 0
    size = len(data)
    info: dict[str, dict] = {}
    while pos < size:
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        header = data[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        parts = header.split()
        if len(parts) >= 2 and parts[1] == "missing":
            continue
        if len(parts) < 3:
            break
        obj_sha, obj_type, obj_len = parts[0], parts[1], int(parts[2])
        body = data[pos : pos + obj_len]
        pos += obj_len + 1  # trailing newline after object body
        if obj_type != "commit":
            continue
        info[obj_sha] = _parse_git_commit(body.decode("utf-8", errors="replace"))
    by_requested: dict[str, dict] = {}
    for sha in candidates:
        if sha in info:
            by_requested[sha] = info[sha]
        else:
            for full_sha, meta in info.items():
                if full_sha.startswith(sha):
                    by_requested[sha] = meta
                    break
    return by_requested


def _parse_git_commit(text: str) -> dict:
    headers_text, _, message = text.partition("\n\n")
    parents: list[str] = []
    author_date: dt.datetime | None = None
    committer_date: dt.datetime | None = None
    for line in headers_text.splitlines():
        if line.startswith("parent "):
            parents.append(line.split(" ", 1)[1].strip())
        elif line.startswith("author "):
            author_date = _parse_git_ident_time(line)
        elif line.startswith("committer "):
            committer_date = _parse_git_ident_time(line)
    subject = message.splitlines()[0].strip() if message.strip() else ""
    return {
        "parents": parents,
        "author_date": author_date,
        "committer_date": committer_date,
        "subject": subject,
    }


def _parse_git_ident_time(line: str) -> dt.datetime | None:
    parts = line.rsplit(" ", 2)
    if len(parts) < 3:
        return None
    try:
        epoch = int(parts[1])
    except ValueError:
        return None
    return dt.datetime.fromtimestamp(epoch, tz=UTC)


def _find_git_repo(start: Path | None) -> Path | None:
    if start is None:
        return None
    cur = Path(start).resolve()
    if cur.is_file():
        cur = cur.parent
    for directory in (cur, *cur.parents):
        if (directory / ".git").exists():
            return directory
    return None


def synthesize_runs(
    raw_points: list[dict],
    *,
    repo: Path | None = None,
    default_state: str = "finished",
) -> list[dict]:
    """Synthesize per-(sha, node) run rows when no `run.parquet` files exist."""
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    dirty_parents: dict[str, str] = {}
    for pt in raw_points:
        by_pair[(pt["sha"], pt["node"])].append(pt)
        if pt.get("_dirty_parent"):
            dirty_parents[pt["sha"]] = pt["_dirty_parent"]

    git_repo = _find_git_repo(repo)
    git_meta = _git_commit_info([sha for sha, _ in by_pair], git_repo) if git_repo else {}

    runs: list[dict] = []
    for (sha, node), pts in sorted(by_pair.items()):
        times = [p["started_at"] for p in pts if p["started_at"] is not None]
        started_at = min(times) if times else None
        finished_at = max(times) if times else None

        suite_ok: dict[str, bool] = {}
        for p in pts:
            suite_ok[p["suite"]] = suite_ok.get(p["suite"], False) or (p["status"] in ("success", "partial"))
        suites_ok = sum(1 for ok in suite_ok.values() if ok)
        suites_failed = sum(1 for ok in suite_ok.values() if not ok)

        meta = git_meta.get(sha, {})
        if sha in dirty_parents:
            base = dirty_parents[sha]
            parents = [base]
            subject = f"dirty working tree on {base[:12]}"
            committer_date = started_at
            author_date = started_at
        else:
            parents = meta.get("parents") or []
            subject = meta.get("subject") or ""
            committer_date = meta.get("committer_date") or started_at
            author_date = meta.get("author_date") or committer_date

        runs.append(
            {
                "sha": sha,
                "parents": parents,
                "author_date": author_date,
                "committer_date": committer_date,
                "subject": subject,
                "branch": "",
                "node": node,
                "state": default_state,
                "reason": None,
                "started_at": started_at,
                "finished_at": finished_at,
                "suites_ok": suites_ok,
                "suites_failed": suites_failed,
                "results": len(pts),
                "ingest_problems": 0,
                "log": "",
            }
        )
    return runs


def read_runs_dir(runs_dir: str | Path) -> tuple[list[dict], list[dict], int]:
    """Read a CI/node `runs/<sha>/<node>/{run.parquet, results.parquet}` or monthly `results/*.parquet` tree."""
    runs_path = check_local_path(runs_dir)
    _require_pyarrow("reading runs/ Parquet files")
    if not runs_path.is_dir():
        raise ViewError(f"runs directory not found: {runs_path}")
    raw_runs = read_runs_metadata(runs_path)
    dedup_runs: dict[tuple[str, str], dict] = {}
    for run in raw_runs:
        dedup_runs[(run["sha"], run["node"])] = run
    runs = list(dedup_runs.values())
    ingest_problems = sum(r["ingest_problems"] for r in runs)
    result_files: list[Path] = []
    monthly_dir = runs_path / "results"
    if monthly_dir.is_dir():
        result_files.extend(sorted(monthly_dir.glob("*.parquet")))
    result_files.extend(sorted(runs_path.glob("*/*/results.parquet")))
    cache: dict[str, str] = {}
    intern = lambda v: cache.setdefault(v, v)  # noqa: E731
    raw_points: list[dict] = []
    for path in result_files:
        raw_points.extend(_read_parquet_points(path, distinguish_dirty=False, intern=intern))
    return runs, _collapse_duplicate_points(raw_points), ingest_problems


def _read_jsonl_points(jsonl_path: Path, *, distinguish_dirty: bool = True) -> list[dict]:
    points: list[dict] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        stored = json.loads(line)
        doc = json.loads(stored["document"])
        order = json.loads(stored["work_order"]) if stored.get("work_order") else None
        pt = _point_from_row(dict(doc, work_order=order), distinguish_dirty=distinguish_dirty)
        if pt is not None:
            points.append(pt)
    return points


def read_store(store_path: str | Path, *, repo: Path | None = None) -> tuple[list[dict], list[dict], int]:
    """Read a single local benchx store (`store.parquet` or `store.jsonl`)."""
    path = check_local_path(store_path)
    if not path.is_file():
        raise ViewError(f"store not found: {path}")
    if path.suffix.lower() in (".jsonl", ".json", ".ndjson"):
        raw_points = _read_jsonl_points(path, distinguish_dirty=True)
    else:
        _require_pyarrow("reading a Parquet store")
        raw_points = _read_parquet_points(path, distinguish_dirty=True)
    collapsed = _collapse_duplicate_points(raw_points)
    runs = synthesize_runs(raw_points, repo=repo)
    counts_by_pair: dict[tuple[str, str], int] = defaultdict(int)
    for pt in collapsed:
        counts_by_pair[(pt["sha"], pt["node"])] += 1
    for run in runs:
        run["results"] = counts_by_pair[(run["sha"], run["node"])]
    return runs, collapsed, 0


def read_results(paths: list[str | Path], *, repo: Path | None = None) -> tuple[list[dict], list[dict], int, dict]:
    """Read JSON result files and work orders via MemoryStore without PyArrow."""
    local_paths = [check_local_path(p) for p in paths]
    store = MemoryStore()
    outcome = store.ingest_paths(local_paths)
    raw_points: list[dict] = []
    for doc in store._documents:
        ref = (doc.get("provenance") or {}).get("info", {}).get("workorder_ref")
        order = store.order(ref) if ref else None
        row = dict(doc, work_order=order)
        pt = _point_from_row(row, distinguish_dirty=True)
        if pt is not None:
            raw_points.append(pt)
    collapsed = _collapse_duplicate_points(raw_points)
    runs = synthesize_runs(raw_points, repo=repo)
    counts_by_pair: dict[tuple[str, str], int] = defaultdict(int)
    for pt in collapsed:
        counts_by_pair[(pt["sha"], pt["node"])] += 1
    for run in runs:
        run["results"] = counts_by_pair[(run["sha"], run["node"])]
    return runs, collapsed, len(outcome["rejected"]), outcome
