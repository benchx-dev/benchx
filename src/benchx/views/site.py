"""Assemble a static benchx dashboard directory and serve it locally over HTTP."""

from __future__ import annotations

import errno
import functools
import http.server
import importlib.util
import json
import re
import shutil
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

from . import inputs, marts as marts_mod, payloads

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = ("app.js", "style.css", "favicon.svg", "logo.svg")
_STAMP_RE = re.compile(r'(href="(style\.css|favicon\.svg)|src="(app\.js))"')


def _has_pyarrow() -> bool:
    if "pyarrow" in sys.modules:
        return sys.modules["pyarrow"] is not None
    try:
        return importlib.util.find_spec("pyarrow") is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _github_commit_url(source_uri: str | None) -> str | None:
    if not source_uri:
        return None
    uri = source_uri.strip()
    if uri.startswith("git@github.com:"):
        uri = "https://github.com/" + uri.removeprefix("git@github.com:")
    if uri.startswith("https://github.com/") or uri.startswith("http://github.com/"):
        base = uri.removesuffix(".git").rstrip("/")
        return f"{base}/commit/{{sha}}"
    return None


def resolve_project(
    *,
    project_file: Path | None = None,
    title: str | None = None,
    config=None,
) -> dict:
    """Build the `project.json` metadata copied beside the view payloads."""
    if project_file is not None:
        path = Path(project_file)
        if not path.is_file():
            raise inputs.ViewError(f"project file not found: {path}")
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise inputs.ViewError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(project, dict):
            raise inputs.ViewError(f"{path}: expected a JSON object")
    else:
        name = config.data.get("project") if config is not None else None
        source_uri = config.section("source").get("uri") if config is not None else None
        project = {
            "name": name or "benchmarks",
            "title": f"{name} benchmarks" if name else "Benchmarks",
            "description": "Benchmarks measured with benchx.",
        }
        if source_uri:
            upstream = {"url": source_uri}
            if commit_url := _github_commit_url(source_uri):
                upstream["commit_url"] = commit_url
            project["upstream"] = upstream

    if title:
        project = {**project, "title": title}
    return project


def _write_static(out: Path, stamp: str) -> None:
    for name in STATIC_FILES:
        shutil.copy2(STATIC_DIR / name, out / name)
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if stamp:
        encoded = quote(stamp, safe="")
        index = _STAMP_RE.sub(rf'\1?v={encoded}"', index)
    (out / "index.html").write_text(index, encoding="utf-8")


def _write_logs(runs: list[dict], data_dir: Path) -> int:
    logs_root = (Path(data_dir) / "logs").resolve()
    written = 0
    for run in runs:
        log = run.get("log") or ""
        if not log:
            continue
        sha_seg = inputs.safe_path_segment(run["sha"])
        node_seg = inputs.safe_path_segment(run["node"])
        log_path = (logs_root / sha_seg / f"{node_seg}.txt").resolve()
        if not log_path.is_relative_to(logs_root):
            raise inputs.ViewError(f"refusing to write log outside {logs_root}: {run['sha']!r}/{run['node']!r}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log, encoding="utf-8")
        written += 1
    return written


def build_site(
    out: Path,
    *,
    store_path: str | Path | None = None,
    results_paths: list[str | Path] | None = None,
    runs_dir: str | Path | None = None,
    project_file: Path | None = None,
    title: str | None = None,
    repo: Path | None = None,
    write_marts: bool | None = None,
    stamp: str | None = None,
    config=None,
) -> dict:
    """Build a complete static benchx dashboard under `out` and return a summary."""
    chosen = sum(x is not None for x in (store_path, results_paths, runs_dir))
    if chosen != 1:
        raise inputs.ViewError("give exactly one of store_path, results_paths, or runs_dir")

    outcome = None
    if runs_dir is not None:
        source = "runs"
        runs, raw_points, ingest_problems = inputs.read_runs_dir(runs_dir)
    elif results_paths is not None:
        source = "results"
        runs, raw_points, ingest_problems, outcome = inputs.read_results(
            list(results_paths), repo=repo
        )
    else:
        source = "store"
        runs, raw_points, ingest_problems = inputs.read_store(store_path, repo=repo)

    if write_marts is None:
        emit_marts = _has_pyarrow()
    else:
        emit_marts = bool(write_marts)

    marts = marts_mod.build_marts(
        runs,
        raw_points,
        ingest_problems=ingest_problems,
        source=source,
        has_parquet_marts=emit_marts,
    )

    out = Path(out).resolve()
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if emit_marts:
        marts_mod.write_parquet_marts(marts, data_dir)

    logs_written = _write_logs(runs, data_dir)
    project = resolve_project(project_file=project_file, title=title, config=config)
    (data_dir / "project.json").write_text(json.dumps(project, indent=1) + "\n", encoding="utf-8")
    (data_dir / "manifest.json").write_text(json.dumps(marts["manifest"], indent=1) + "\n", encoding="utf-8")
    view_counts = payloads.build_views(marts, data_dir)
    _write_static(out, stamp if stamp is not None else marts["manifest"]["built_at"])

    return {
        "out": str(out),
        "source": source,
        "counts": marts["manifest"]["counts"],
        "views": view_counts,
        "marts": emit_marts,
        "logs": logs_written,
        "outcome": outcome,
    }


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return


def create_server(
    directory: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    fallback_port: bool = False,
) -> tuple[http.server.ThreadingHTTPServer, str]:
    """Bind a local HTTP server for `directory` without starting its loop."""
    handler = functools.partial(_QuietHandler, directory=str(Path(directory).resolve()))
    try:
        server = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if fallback_port and exc.errno == errno.EADDRINUSE and port != 0:
            server = http.server.ThreadingHTTPServer((host, 0), handler)
        else:
            raise
    bound_host, bound_port = server.server_address[:2]
    display_host = "127.0.0.1" if bound_host in ("", "0.0.0.0") else bound_host
    return server, f"http://{display_host}:{bound_port}/"


def serve_site(
    directory: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    fallback_port: bool = False,
    open_browser: bool = False,
    announce=print,
) -> None:
    """Serve `directory` over local HTTP until interrupted with Ctrl-C."""
    server, url = create_server(directory, host=host, port=port, fallback_port=fallback_port)
    announce(f"serving {Path(directory).resolve()} at {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
