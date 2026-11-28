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

from . import inputs, payloads
from . import marts as marts_mod

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = (
    "app.js",
    "style.css",
    "favicon.svg",
    "logo.svg",
    # uPlot is vendored so charts render offline (gh-129); LICENSE ships with it (MIT).
    "vendor/uplot/uPlot.iife.min.js",
    "vendor/uplot/uPlot.min.css",
    "vendor/uplot/LICENSE",
)
_STAMP_RE = re.compile(
    r'(href="(style\.css|favicon\.svg|vendor/uplot/uPlot\.min\.css)'
    r'|src="(app\.js|vendor/uplot/uPlot\.iife\.min\.js))"'
)


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
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(STATIC_DIR / name, out / name)
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if stamp:
        encoded = quote(stamp, safe="")
        index = _STAMP_RE.sub(rf'\1?v={encoded}"', index)
    (out / "index.html").write_text(index, encoding="utf-8")


def _write_logs(runs: list[dict], data_dir: Path) -> int:
    logs_root = (Path(data_dir) / "logs").resolve()
    if logs_root.is_dir():
        shutil.rmtree(logs_root)
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
    views_format: str = "json",
    stamp: str | None = None,
    config=None,
) -> dict:
    """Build a complete static benchx dashboard under `out` and return a summary."""
    chosen = sum(x is not None for x in (store_path, results_paths, runs_dir))
    if chosen != 1:
        raise inputs.ViewError("give exactly one of store_path, results_paths, or runs_dir")
    if views_format not in payloads.VIEWS_FORMATS:
        raise inputs.ViewError(
            f"unsupported views_format {views_format!r}; expected one of {', '.join(payloads.VIEWS_FORMATS)}"
        )

    # Validate project metadata and optional PyArrow requirements before touching `out`.
    project = resolve_project(project_file=project_file, title=title, config=config)
    if write_marts is None:
        emit_marts = _has_pyarrow()
    else:
        emit_marts = bool(write_marts)
    if emit_marts:
        inputs._require_pyarrow("writing Parquet marts")
    if views_format == "parquet":
        inputs._require_pyarrow("writing Parquet views")

    outcome = None
    if runs_dir is not None:
        source = "runs"
        runs, raw_points, ingest_problems = inputs.read_runs_dir(runs_dir)
    elif results_paths is not None:
        source = "results"
        runs, raw_points, ingest_problems, outcome = inputs.read_results(list(results_paths), repo=repo)
    else:
        source = "store"
        runs, raw_points, ingest_problems = inputs.read_store(store_path, repo=repo)

    marts = marts_mod.build_marts(
        runs,
        raw_points,
        ingest_problems=ingest_problems,
        source=source,
        has_parquet_marts=emit_marts,
    )
    del raw_points
    marts["manifest"]["views_format"] = views_format

    out = Path(out).resolve()
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    if emit_marts:
        marts_mod.write_parquet_marts(marts, data_dir)
    else:
        for name in ("commits", "points", "changes", "latest", "movers"):
            stale = data_dir / f"{name}.parquet"
            if stale.exists():
                stale.unlink()

    logs_written = _write_logs(runs, data_dir)
    (data_dir / "project.json").write_text(json.dumps(project, indent=1) + "\n", encoding="utf-8")
    (data_dir / "manifest.json").write_text(json.dumps(marts["manifest"], indent=1) + "\n", encoding="utf-8")
    view_counts = payloads.build_views(marts, data_dir, views_format=views_format)
    _write_static(out, stamp if stamp is not None else marts["manifest"]["built_at"])

    return {
        "out": str(out),
        "source": source,
        "counts": marts["manifest"]["counts"],
        "views": view_counts,
        "views_format": views_format,
        "marts": emit_marts,
        "logs": logs_written,
        "outcome": outcome,
    }


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        return

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        range_hdr = self.headers.get("Range")
        if not range_hdr:
            self._range = None
            return super().send_head()

        path = self.translate_path(self.path)
        if not Path(path).is_file():
            self._range = None
            return super().send_head()

        m = _RANGE_RE.match(range_hdr.strip())
        if not m:
            self._range = None
            return super().send_head()

        f = open(path, "rb")
        try:
            fs = Path(path).stat()
            size = fs.st_size
            s_str, e_str = m.group(1), m.group(2)
            if s_str == "" and e_str == "":
                raise ValueError("empty range")
            if s_str == "":
                # Suffix byte-range: last N bytes.
                suffix = int(e_str)
                start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(s_str)
                end = int(e_str) if e_str else size - 1
            if start >= size or end < start:
                f.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()
            f.seek(start)
            self._range = length
            return f
        except Exception:
            f.close()
            self._range = None
            return super().send_head()

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range", None)
        if remaining is None:
            super().copyfile(source, outputfile)
            return
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


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
