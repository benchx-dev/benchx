"""Static dashboard views and local report server (`bx view`).

Derives flat tables (marts) and view JSON payloads from a local Parquet store,
JSON result files, or a CI `runs/<sha>/<node>/` directory, and bundles a static
HTML/JS dashboard that can be served locally or deployed to static hosting.
"""

from .inputs import (
    ViewError,
    parse_time,
    read_results,
    read_runs_dir,
    read_runs_metadata,
    read_store,
    run_schema,
    write_run,
)
from .marts import (
    MOVERS_PER_COMMIT,
    RECENT,
    build_changes,
    build_commits,
    build_latest,
    build_marts,
    build_movers,
    build_points,
    write_parquet_marts,
)
from .payloads import (
    MOVERS,
    NOISIEST,
    NOTABLE,
    RECENT_COMMITS,
    SPARK_POINTS,
    build_views,
    last_executed,
    newest_commits,
    signed,
)
from .site import STATIC_DIR, build_site, create_server, resolve_project, serve_site

__all__ = [
    "MOVERS",
    "MOVERS_PER_COMMIT",
    "NOISIEST",
    "NOTABLE",
    "RECENT",
    "RECENT_COMMITS",
    "SPARK_POINTS",
    "STATIC_DIR",
    "ViewError",
    "build_changes",
    "build_commits",
    "build_latest",
    "build_marts",
    "build_movers",
    "build_points",
    "build_site",
    "build_views",
    "create_server",
    "last_executed",
    "newest_commits",
    "parse_time",
    "read_results",
    "read_runs_dir",
    "read_runs_metadata",
    "read_store",
    "resolve_project",
    "run_schema",
    "serve_site",
    "signed",
    "write_parquet_marts",
    "write_run",
]
