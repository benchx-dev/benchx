"""Tests for `bx view --cli`, `bx series` measurand/parameter display, and `bx history --repo`."""

import json
import sys

from benchx import cli, views
from benchx.store import Store
from conftest import QUIET, NOISY, cases, git, make_build, requires_pyarrow


def _block_pyarrow(monkeypatch):
    for mod in list(sys.modules):
        if mod == "pyarrow" or mod.startswith("pyarrow."):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setitem(sys.modules, "pyarrow", None)


def _ingest_two_commits(repo, tmp, *, store_name="store.jsonl", measure_head_first=False):
    """Benchmark base and head commits into a local store (optionally measuring head first)."""
    base_build = make_build(tmp / "b-base", repo["wt-base"], cases(quiet=1.00e-3, noisy=2.00e-3))
    head_build = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.20e-3, noisy=1.80e-3))
    store_path = tmp / store_name

    # Put store_path in benchx.conf.json so CLI commands use it consistently.
    (tmp / "benchx.conf.json").write_text(json.dumps({
        "schema_version": "benchx/config/0.1.0",
        "project": "demo",
        "paths": {"store": store_name},
    }) + "\n")

    order_pairs = [
        ("base", base_build, repo["wt-base"]),
        ("head", head_build, repo["wt-head"]),
    ]
    if measure_head_first:
        order_pairs.reverse()

    for label, build, wt in order_pairs:
        assert cli.main([
            "bench", f"{build}:{wt}",
            "--adapter", "google-benchmark",
            "--suite", "demo-bench",
            "--run-key", f"run-{label}",
            "--out", str(tmp / f"out-{label}"),
        ]) == cli.EXIT_OK

    return store_path


def test_view_cli_changes_movers_latest_commits_arrow_free(repo, tmp, monkeypatch, capsys):
    """`bx view --cli` renders changes, movers, latest, commits, and --json without PyArrow."""
    _block_pyarrow(monkeypatch)
    _ingest_two_commits(repo, tmp, store_name="store.jsonl")
    capsys.readouterr()

    # Default --cli prints changes with heuristic note and git commit transition.
    assert cli.main(["view", "--cli", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "heuristic; not a comparator verdict" in out
    assert f"{repo['base'][:8]}->{repo['head'][:8]}" in out
    assert QUIET in out and NOISY in out
    assert "+20." in out and "-" in out

    # Movers sorts by largest |rel_change| first (QUIET ~+20% before NOISY ~-10%).
    assert cli.main(["view", "--cli", "movers", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    movers_out = capsys.readouterr().out
    assert movers_out.index(QUIET) < movers_out.index(NOISY)

    # --limit caps the number of rows.
    assert cli.main(["view", "--cli", "movers", "-n", "1", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    limited = capsys.readouterr().out
    assert QUIET in limited and NOISY not in limited

    # --workload filters by regex.
    assert cli.main(["view", "--cli", "changes", "--workload", "noisy", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    filtered = capsys.readouterr().out
    assert NOISY in filtered and QUIET not in filtered

    # --commit accepts both SHA prefix and git ref (HEAD).
    assert cli.main(["view", "--cli", "changes", "--commit", "HEAD", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    assert QUIET in capsys.readouterr().out
    assert cli.main(["view", "--cli", "changes", "--commit", repo["base"][:8], "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    assert "(no changes to display)" in capsys.readouterr().out

    # --json returns structured mart rows.
    assert cli.main(["view", "--cli", "changes", "--json", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    by_case = {r["case"]: r for r in rows}
    assert 0.15 < by_case[QUIET]["rel_change"] < 0.25
    assert -0.25 < by_case[NOISY]["rel_change"] < 0.0

    # latest & commits views.
    assert cli.main(["view", "--cli", "latest", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    latest_out = capsys.readouterr().out
    assert "vs_prev" in latest_out and QUIET in latest_out and "+20." in latest_out

    assert cli.main(["view", "--cli", "commits", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    commits_out = capsys.readouterr().out
    assert repo["head"][:8] in commits_out and repo["base"][:8] in commits_out
    assert commits_out.index(repo["head"][:8]) < commits_out.index(repo["base"][:8])


def test_view_cli_flag_validation(tmp, capsys):
    """Conflicting site/CLI flags and invalid regexes are refused cleanly."""
    res_dir = tmp / "empty-res"
    res_dir.mkdir()

    # Site flags with --cli are refused.
    assert cli.main(["view", "--results", str(res_dir), "--cli", "--out", str(tmp / "site")]) == cli.EXIT_REFUSED
    assert "cannot be combined with --cli" in capsys.readouterr().err

    assert cli.main(["view", "--results", str(res_dir), "--cli", "--serve"]) == cli.EXIT_REFUSED
    assert "cannot be combined with --cli" in capsys.readouterr().err

    # CLI-only flags without --cli are refused.
    assert cli.main(["view", "--results", str(res_dir), "--commit", "HEAD"]) == cli.EXIT_REFUSED
    assert "go with --cli" in capsys.readouterr().err

    # Invalid workload regex is refused.
    assert cli.main(["view", "--results", str(res_dir), "--cli", "--workload", "[invalid"]) == cli.EXIT_REFUSED
    assert "invalid --workload regex" in capsys.readouterr().err


def test_series_hides_conditions_by_default_and_shows_parameters(tmp, capsys):
    """`bx series` defaults to measurands, prints workload parameters, and `--all` includes conditions."""
    store = Store(tmp / "store.jsonl")
    (tmp / "benchx.conf.json").write_text(json.dumps({
        "schema_version": "benchx/config/0.1.0",
        "project": "demo",
        "paths": {"store": "store.jsonl"},
    }) + "\n")

    def _doc(ingest_key, quantity, unit, kind, params):
        return {
            "schema_version": "benchx/measurement-result/0.1.0",
            "producer": {"name": "benchx/test", "version": "0.1.0", "mapping_version": "test/v1"},
            "ingest_key": ingest_key,
            "attempt_key": "att-1",
            "project": "demo",
            "source": {"uri": "https://example.org/demo.git", "type": "git"},
            "revision": {"key": "a" * 40},
            "benchmark": {"source": {"uri": "https://example.org/demo.git", "type": "git"}, "revision": {"key": "a" * 40}},
            "coordinates": {
                "workload": {"name": "bench_convert", "parameters": params},
                "subject": {"name": "demo", "components": [{"role": "primary"}]},
                "protocol": {"name": "pyperf", "version": "1"},
                "environment": {"schema_version": "machine/v1", "identity": {"hostname": "h1"}},
            },
            "measurement": {
                "kind": kind,
                "quantity": {"name": quantity, "unit": unit, "direction": "lower-is-better"},
                "status": "success",
                "observations": [0.01, 0.011],
            },
            "provenance": {
                "run_key": "r1",
                "started_at": "2026-11-01T10:00:00Z",
                "subject_dirty": "clean",
                "subject_tree": "b" * 40,
                "benchmark_dirty": "clean",
                "benchmark_tree": "b" * 40,
            },
        }

    docs = [
        ("m1.json", json.dumps(_doc("r1:m1", "wall-time", "s", "measurand", {"type": "'int64'"}))),
        ("m2.json", json.dumps(_doc("r1:m2", "wall-time", "s", "measurand", {"type": "'float64'"}))),
        ("c1.json", json.dumps(_doc("r1:c1", "load-average", "1", "condition", {"type": "'int64'"}))),
    ]
    # Use separate attempt_keys for distinct parameter assignments.
    fixed_docs = []
    for idx, (name, raw) in enumerate(docs):
        obj = json.loads(raw)
        obj["attempt_key"] = f"att-{0 if idx in (0, 2) else 1}"
        fixed_docs.append((name, json.dumps(obj)))
    outcome = store.ingest(fixed_docs)
    assert outcome["ingested"] == 3 and not outcome["rejected"]

    # Default `bx series` hides load-average and displays parameters.
    assert cli.main(["series"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "wall-time" in out
    assert "load-average" not in out
    assert "bench_convert [type='int64']" in out
    assert "bench_convert [type='float64']" in out

    # Explicit --quantity load-average still finds condition series.
    assert cli.main(["series", "--quantity", "load-average"]) == cli.EXIT_OK
    assert "load-average" in capsys.readouterr().out

    # --all includes both measurands and conditions.
    assert cli.main(["series", "--all"]) == cli.EXIT_OK
    all_out = capsys.readouterr().out
    assert "wall-time" in all_out and "load-average" in all_out


def test_history_orders_by_git_ancestry_with_repo(repo, tmp, monkeypatch, capsys):
    """`bx history --repo` orders points by git ancestry even when head was measured before base."""
    _block_pyarrow(monkeypatch)
    store_path = _ingest_two_commits(repo, tmp, store_name="store.jsonl", measure_head_first=True)
    store = Store(store_path)
    fp = next(
        s["fingerprint"][:12]
        for s in store.series(workload=QUIET, quantity="wall-time")
        if "benchx/median/v1" in s["estimator"]
    )
    capsys.readouterr()

    # Without --repo (tmp is not a git repo), points fall back to measurement start (head first).
    assert cli.main(["history", fp]) == cli.EXIT_OK
    lines_no_repo = capsys.readouterr().out.splitlines()
    assert "revision order unavailable" in lines_no_repo[0]
    assert lines_no_repo[1].startswith(repo["head"][:12])
    assert lines_no_repo[2].startswith(repo["base"][:12])

    # With --repo, points are ordered by git ancestry (base before head).
    assert cli.main(["history", fp, "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    lines_repo = capsys.readouterr().out.splitlines()
    assert lines_repo[0] == "ordered by git revision ancestry"
    assert lines_repo[1].startswith(repo["base"][:12])
    assert lines_repo[2].startswith(repo["head"][:12])


@requires_pyarrow
def test_view_cli_with_parquet_store(repo, tmp, capsys):
    """`bx view --cli` works against store.parquet as well."""
    _ingest_two_commits(repo, tmp, store_name="store.parquet")
    capsys.readouterr()
    assert cli.main(["view", "--cli", "changes", "--repo", str(repo["wt-head"])]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert QUIET in out and "+20." in out
