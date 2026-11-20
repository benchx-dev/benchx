"""Derived tables, JSON view payloads, static site assembly, and `bx view` CLI."""

import datetime as dt
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from benchx import cli, views
from benchx.views import inputs, marts as marts_mod, payloads, site
from conftest import NOISY, QUIET, cases, make_build, requires_pyarrow, run_rounds

UTC = dt.timezone.utc
SHAS = [f"{i}" * 40 for i in range(1, 5)]


def _block_pyarrow(monkeypatch):
    for name in list(sys.modules):
        if name == "pyarrow" or name.startswith("pyarrow.") or name in ("benchx.store", "benchx.parquet"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)


def _point(sha, order, parent, case, estimate, status="success", quantity="wall-time", direction="lower-is-better"):
    return {
        "sha": sha,
        "commit_order": order,
        "committer_date": dt.datetime(2026, 1, order, tzinfo=UTC),
        "parent": parent,
        "node": "n1",
        "suite": "s",
        "case": case,
        "params": "",
        "quantity": quantity,
        "unit": "s",
        "direction": direction,
        "status": status,
        "reason": "" if status == "success" else "harness.no-output",
        "estimate": estimate,
        "mean": estimate,
        "min": estimate,
        "max": estimate,
        "n": 1 if estimate is not None else 0,
        "started_at": dt.datetime(2026, 1, order, tzinfo=UTC),
        "run_key": f"main/{sha}/n1",
        "ingest_key": f"{sha}:{case}",
    }


def _by(rows, **keys):
    out = [r for r in rows if all(r[k] == v for k, v in keys.items())]
    assert len(out) == 1, (keys, out)
    return out[0]


# --- Marts arithmetic (pure Python, no PyArrow required) -------------------------------

@pytest.fixture
def synthetic_marts():
    pts = [
        _point("c1", 1, "c0", "steady", 1.00),
        _point("c2", 2, "c1", "steady", 1.01),
        _point("c3", 3, "c2", "steady", 1.10),
        _point("c1", 1, "c0", "quiet", 2.00),
        _point("c3", 3, "c2", "quiet", 2.02),
        _point("c3", 3, "c2", "broken", None, status="error"),
    ]
    changes = marts_mod.build_changes(pts)
    latest = marts_mod.build_latest(pts)
    movers = marts_mod.build_movers(changes)
    return {"changes": changes, "latest": latest, "movers": movers}


def test_changes_follow_the_series_and_record_gaps(synthetic_marts):
    ch = synthetic_marts["changes"]
    assert len(ch) == 3  # steady: c2, c3; quiet: c3; broken has no successful point
    c3 = _by(ch, case="steady", sha="c3")
    assert c3["prev_sha"] == "c2" and c3["prev_is_parent"] is True and c3["gap"] == 1
    assert c3["rel_change"] == pytest.approx(1.10 / 1.01 - 1)
    q3 = _by(ch, case="quiet", sha="c3")
    assert q3["prev_sha"] == "c1" and q3["prev_is_parent"] is False and q3["gap"] == 2
    assert q3["rel_change"] == pytest.approx(0.01)


def test_latest_summarizes_each_series(synthetic_marts):
    lt = synthetic_marts["latest"]
    assert {r["case"] for r in lt} == {"steady", "quiet"}
    s = _by(lt, case="steady")
    assert s["sha"] == "c3" and s["points"] == 3 and s["first_order"] == 1
    assert s["estimate"] == pytest.approx(1.10)
    assert s["vs_prev"] == pytest.approx(1.10 / 1.01 - 1)
    assert s["vs_10"] is None and s["vs_100"] is None
    assert s["noise"] == pytest.approx((abs(1.10 / 1.01 - 1) + 0.01) / 2)
    q = _by(lt, case="quiet")
    assert q["noise"] is None
    assert s["noise_rank"] == 0.0 and q["noise_rank"] == 1.0


def test_movers_rank_changes_per_commit(synthetic_marts):
    mv = synthetic_marts["movers"]
    c3 = [r for r in mv if r["sha"] == "c3"]
    assert [r["case"] for r in c3] == ["steady", "quiet"]
    assert all(r["rel_change"] is not None for r in mv)


def test_build_commits_orders_by_committer_date():
    runs = [
        {"sha": "b" * 40, "parents": ["a" * 40], "committer_date": inputs.parse_time("2026-02-01T10:00:00+00:00"),
         "author_date": inputs.parse_time("2026-02-01T10:00:00+00:00"), "subject": "second", "node": "n1",
         "state": "finished", "results": 5},
        {"sha": "a" * 40, "parents": ["9" * 40], "committer_date": inputs.parse_time("2026-01-01T10:00:00+01:00"),
         "author_date": inputs.parse_time("2026-01-01T10:00:00+01:00"), "subject": "first", "node": "n1",
         "state": "finished", "results": 5},
    ]
    commits = marts_mod.build_commits(runs)
    order = {r["sha"]: r["commit_order"] for r in commits}
    assert order["a" * 40] == 1 and order["b" * 40] == 2
    assert _by(commits, sha="a" * 40)["parent"] == "9" * 40


# --- View payloads (pure Python, no PyArrow required) ----------------------------------

def _build_synthetic_views(tmp_path: Path, raw_points: list[dict], n_commits: int = 4) -> Path:
    runs = [
        {
            "sha": SHAS[i - 1],
            "parents": [SHAS[i - 2] if i > 1 else "0" * 40],
            "committer_date": dt.datetime(2026, 1, i, tzinfo=UTC),
            "author_date": dt.datetime(2026, 1, i, tzinfo=UTC),
            "subject": f"commit {i}",
            "branch": "main",
            "node": "n1",
            "state": "finished",
            "reason": None,
            "started_at": dt.datetime(2026, 1, i, tzinfo=UTC),
            "finished_at": dt.datetime(2026, 1, i, tzinfo=UTC),
            "suites_ok": 1,
            "suites_failed": 0,
            "results": 3,
            "ingest_problems": 0,
            "log": "",
        }
        for i in range(1, n_commits + 1)
    ]
    marts = marts_mod.build_marts(runs, raw_points, source="runs")
    data = tmp_path / "data"
    data.mkdir()
    payloads.build_views(marts, data)
    return data / "views"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def view_out(tmp_path):
    pts = [
        _point(SHAS[0], 1, "0" * 40, "flaky", None, "error"),
        _point(SHAS[1], 2, SHAS[0], "flaky", 1.0),
        _point(SHAS[2], 3, SHAS[1], "flaky", None, "error"),
        _point(SHAS[3], 4, SHAS[2], "flaky", 1.1),
        *[_point(SHAS[i - 1], i, SHAS[i - 2] if i > 1 else "0" * 40, "broken", None, "error") for i in range(1, 5)],
        *[_point(SHAS[i - 1], i, SHAS[i - 2] if i > 1 else "0" * 40, "fast", v,
                 quantity="items-per-second", direction="higher-is-better")
          for i, v in [(1, 100), (2, 101), (3, 100), (4, 80)]],
    ]
    return _build_synthetic_views(tmp_path, pts)


def test_history_keeps_each_estimate_on_its_commit(view_out):
    flaky = _load(view_out / "history" / "s.json")["series"]["flaky||wall-time|n1"]
    assert flaky["orders"] == [1, 2, 3, 4]
    assert flaky["estimates"] == [None, 1.0, None, 1.1]
    assert flaky["statuses"] == ["error", "success", "error", "success"]
    spark = next(s for s in _load(view_out / "sparks.json")["series"] if s["case"] == "flaky")
    assert spark["orders"] == [2, 4] and spark["estimates"] == [1.0, 1.1]


def test_failures_are_listed_not_hidden(view_out):
    series = {s["case"]: s for s in _load(view_out / "series.json")}
    assert series["broken"]["estimate"] is None and series["broken"]["status"] == "error" and series["broken"]["points"] == 0
    assert series["flaky"]["status"] == "success"
    landing = _load(view_out / "landing.json")
    assert [f["case"] for f in landing["failures"]] == ["broken"]
    assert landing["latest_runs"][0]["failed_results"] == 1
    newest = _load(view_out / "commits" / f"{SHAS[3]}.json")
    assert [f["case"] for f in newest["failures"]] == ["broken"]


def test_counts_follow_the_direction(view_out):
    newest = _load(view_out / "landing.json")["latest_runs"][0]
    assert newest["regressed"] == 2 and newest["improved"] == 0
    assert {m["case"] for m in _load(view_out / "landing.json")["movers"]["regressed"]} == {"flaky", "fast"}


def test_no_results_still_builds(tmp_path):
    out = _build_synthetic_views(tmp_path, [], n_commits=2)
    landing = _load(out / "landing.json")
    assert landing["stats"]["results"] == 0 and len(landing["recent_commits"]) == 2
    assert _load(out / "series.json") == [] and _load(out / "sparks.json") == {"axis": [], "series": []}
    assert _load(out / "commits" / f"{SHAS[1]}.json")["changes"] == []


def test_last_executed_is_by_finish_time_not_commit_order():
    commits = [
        {"sha": "new", "node": "n1", "finished_at": "2026-01-02T10:00:00+00:00", "started_at": None},
        {"sha": "old", "node": "n1", "finished_at": "2026-01-03T09:00:00+00:00", "started_at": None},
        {"sha": "new", "node": "n2", "finished_at": None, "started_at": "2026-01-01T08:00:00+00:00"},
    ]
    assert payloads.newest_commits(commits)["n1"]["sha"] == "new"
    last = payloads.last_executed(commits)
    assert last["n1"]["sha"] == "old" and last["n2"]["sha"] == "new"


# --- End-to-end JSON results mode (Arrow-free) -----------------------------------------

def test_view_from_json_results_without_pyarrow(repo, tmp, monkeypatch, capsys):
    _block_pyarrow(monkeypatch)
    base = make_build(tmp / "build-base", repo["wt-base"], cases())
    head = make_build(tmp / "build-head", repo["wt-head"], cases(quiet=1.10e-3, noisy=2.00e-3))

    out_base = tmp / "res-base"
    out_head = tmp / "res-head"
    out_dirty = tmp / "res-dirty"
    for target, out_dir, rkey in (
        (f"{base}:{repo['wt-base']}", out_base, "r-base"),
        (f"{head}:{repo['wt-head']}", out_head, "r-head"),
    ):
        assert cli.main([
            "bench", target, "--adapter", "google-benchmark", "--suite", "demo-bench",
            "--run-key", rkey, "--out", str(out_dir), "--no-ingest",
        ]) == cli.EXIT_OK

    # Make an uncommitted edit on wt-head and benchmark it too.
    (repo["wt-head"] / "bench.cpp").write_text("// dirty edit\n")
    assert cli.main([
        "bench", f"{head}:{repo['wt-head']}", "--adapter", "google-benchmark", "--suite", "demo-bench",
        "--run-key", "r-dirty", "--out", str(out_dirty), "--no-ingest",
    ]) == cli.EXIT_OK
    capsys.readouterr()

    site_dir = tmp / "site"
    assert cli.main([
        "view",
        "--results", str(out_base), str(out_head), str(out_dirty),
        "--repo", str(repo["src"]),
        "--title", "Demo Local",
        "--stamp", "v1",
        "--out", str(site_dir),
    ]) == cli.EXIT_OK
    stdout = capsys.readouterr().out
    assert "3 commit(s)" in stdout

    # Static assets stamped and present, no Parquet marts written without PyArrow.
    html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert 'href="style.css?v=v1"' in html and 'src="app.js?v=v1"' in html
    for asset in ("app.js", "style.css", "favicon.svg", "logo.svg"):
        assert (site_dir / asset).is_file()

    manifest = _load(site_dir / "data" / "manifest.json")
    assert manifest["source"] == "results"
    assert manifest["tables"] == {}
    assert manifest["counts"]["distinct_commits"] == 3
    assert manifest["counts"]["points"] == 6

    project = _load(site_dir / "data" / "project.json")
    assert project["title"] == "Demo Local"

    landing = _load(site_dir / "data" / "views" / "landing.json")
    subjects = {c["sha"]: c["subject"] for c in landing["recent_commits"]}
    assert subjects[repo["base"]] == "base"
    assert subjects[repo["head"]] == "head"
    dirty_shas = [sha for sha in subjects if "-dirty-" in sha]
    assert len(dirty_shas) == 1
    assert "dirty working tree" in subjects[dirty_shas[0]]

    # Head's parent is base, verified from git metadata.
    head_page = _load(site_dir / "data" / "views" / "commits" / f"{repo['head']}.json")
    assert head_page["parent"] == repo["base"]
    assert all(ch["prev_is_parent"] is True for ch in head_page["changes"])


def test_view_pyarrow_errors_are_actionable(tmp, monkeypatch, capsys):
    _block_pyarrow(monkeypatch)
    assert cli.main(["view", "--out", str(tmp / "site")]) == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "benchx[parquet]" in err and "--results" in err

    res = tmp / "empty"
    res.mkdir()
    assert cli.main(["view", "--results", str(res), "--marts", "--out", str(tmp / "site2")]) == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "benchx[parquet]" in err


# --- End-to-end Parquet store & CI runs/ modes -----------------------------------------

@requires_pyarrow
def test_view_from_local_store_collapses_rounds_and_writes_marts(repo, tmp, capsys):
    import pyarrow.parquet as pq
    from benchx.store import Store

    base = make_build(tmp / "b-base", repo["wt-base"], cases())
    head = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.10e-3, noisy=2.00e-3))
    out = run_rounds(
        tmp,
        [{"build": base, "source": repo["wt-base"]}, {"build": head, "source": repo["wt-head"]}],
        rounds=3,
        run_key="cmp-1",
        out=tmp / "results-cmp",
    )
    store = Store(tmp / "store.parquet")
    assert store.ingest_paths([out])["rejected"] == []

    site_dir = tmp / "site"
    assert cli.main([
        "view", "--store", str(tmp / "store.parquet"),
        "--repo", str(repo["src"]), "--out", str(site_dir),
    ]) == cli.EXIT_OK

    manifest = _load(site_dir / "data" / "manifest.json")
    assert manifest["source"] == "store"
    assert set(manifest["tables"]) == {"commits", "points", "changes", "latest", "movers"}
    # 2 commits x 2 cases collapsed across 3 rounds (5 reps each -> n=15 per commit).
    assert manifest["counts"]["points"] == 4
    points = pq.read_table(site_dir / "data" / "points.parquet").to_pylist()
    assert len(points) == 4 and all(p["n"] == 15 for p in points)
    assert {p["case"] for p in points} == {QUIET, NOISY}


@requires_pyarrow
def test_view_from_runs_dir_with_logs_and_failed_build(repo, tmp, capsys):
    import pyarrow.parquet as pq
    from benchx.store import Store

    runs_dir = tmp / "runs"
    base = make_build(tmp / "b-base", repo["wt-base"], cases())
    head = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.12e-3, noisy=2.00e-3))

    for sha, parent, date, build_dir, wt in (
        (repo["base"], "0" * 40, "2026-01-01T10:00:00+00:00", base, repo["wt-base"]),
        (repo["head"], repo["base"], "2026-01-02T10:00:00+00:00", head, repo["wt-head"]),
    ):
        res_out = tmp / f"raw-{sha[:8]}"
        assert cli.main([
            "bench", f"{build_dir}:{wt}", "--adapter", "google-benchmark", "--suite", "demo-bench",
            "--run-key", f"main/{sha}/m5", "--out", str(res_out), "--no-ingest",
        ]) == cli.EXIT_OK
        node_dir = runs_dir / sha / "m5"
        Store(node_dir / "results.parquet").ingest_paths([res_out])
        views.write_run(
            node_dir,
            {"sha": sha, "parents": [parent], "committer_date": date, "author_date": date, "subject": f"msg {sha[:6]}"},
            {"state": "finished", "node": "m5", "suites_ok": 1, "suites_failed": 0, "suites": {"demo-bench": {"results": 2}}},
            log=f"log for {sha[:6]}\n",
        )

    # A third commit whose build failed (run.parquet only, no results.parquet).
    failed_sha = "f" * 40
    views.write_run(
        runs_dir / failed_sha / "m5",
        {"sha": failed_sha, "parents": [repo["head"]], "committer_date": "2026-01-03T10:00:00+00:00", "subject": "broken build"},
        {"state": "failed", "node": "m5"},
        log="cmake failed\n",
        ingest_problems=1,
    )

    project_file = tmp / "project.json"
    project_file.write_text(json.dumps({
        "name": "demo",
        "title": "Demo CI Benchmarks",
        "repository": "https://github.com/example/benchx-demo",
        "upstream": {"url": "https://github.com/example/demo.git", "branch": "main",
                     "commit_url": "https://github.com/example/demo/commit/{sha}"},
    }))
    capsys.readouterr()

    site_dir = tmp / "site"
    assert cli.main([
        "view", "--runs", str(runs_dir), "--project-file", str(project_file), "--out", str(site_dir),
    ]) == cli.EXIT_OK

    manifest = _load(site_dir / "data" / "manifest.json")
    assert manifest["source"] == "runs"
    assert manifest["counts"]["distinct_commits"] == 3
    assert manifest["counts"]["ingest_problems"] == 1
    assert (site_dir / "data" / "logs" / failed_sha / "m5.txt").read_text(encoding="utf-8") == "cmake failed\n"

    failed_page = _load(site_dir / "data" / "views" / "commits" / f"{failed_sha}.json")
    assert failed_page["nodes"][0]["state"] == "failed" and failed_page["nodes"][0]["has_log"] is True
    assert pq.read_table(site_dir / "data" / "commits.parquet").num_rows == 3


# --- Local HTTP serving & default temp-dir behavior ------------------------------------

def test_serve_and_default_temp_dir(tmp, monkeypatch):
    res_dir = tmp / "empty-res"
    res_dir.mkdir()
    site_dir = tmp / "built"
    views.build_site(site_dir, results_paths=[res_dir], write_marts=False)

    server, url = site.create_server(site_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(url) as resp:
            assert resp.status == 200
            assert b"Benchmarks" in resp.read()
        with urllib.request.urlopen(url + "data/manifest.json") as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["source"] == "results"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    # Without --out, `bx view` builds into a temporary directory and serves it.
    served_dirs = []

    def fake_serve(directory, **kwargs):
        assert (Path(directory) / "index.html").is_file()
        assert (Path(directory) / "data" / "views" / "landing.json").is_file()
        served_dirs.append(Path(directory))

    monkeypatch.setattr(views, "serve_site", fake_serve)
    assert cli.main(["view", "--results", str(res_dir)]) == cli.EXIT_OK
    assert len(served_dirs) == 1
    assert not served_dirs[0].exists()  # temp directory cleaned up after serve exits


def test_parametrized_workload_and_rejected_results(adhoc, tmp, monkeypatch, capsys):
    _block_pyarrow(monkeypatch)
    res_dir = tmp / "param-res"
    res_dir.mkdir()
    doc = json.loads(json.dumps(adhoc))
    doc["coordinates"]["workload"]["parameters"] = {"b": 2, "a": 1}
    (res_dir / "result-1.json").write_text(json.dumps(doc), encoding="utf-8")

    site_dir = tmp / "param-site"
    assert cli.main(["view", "--results", str(res_dir), "--out", str(site_dir)]) == cli.EXIT_OK
    series = _load(site_dir / "data" / "views" / "series.json")
    assert len(series) == 1
    assert series[0]["params"] == '{"a":1,"b":2}'

    # Malformed JSON file is reported and exits EXIT_INVALID.
    (res_dir / "broken.json").write_text("{not valid", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["view", "--results", str(res_dir), "--out", str(tmp / "bad-site")]) == cli.EXIT_INVALID
    assert "rejected broken.json: malformed" in capsys.readouterr().err
