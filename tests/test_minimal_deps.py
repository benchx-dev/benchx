"""Minimal-dependency behavior: optional PyArrow, narrowed format checkers, and RFC 6901 pointers."""

import importlib
import json
import subprocess
import sys

import pytest
from jsonschema import FormatChecker

from benchx import cli, core, identity
from benchx.core import catalog
from benchx.core.errors import StructureError
from benchx.results import MemoryStore, store_project
from conftest import NOISY, QUIET, cases, git, make_build, order, requires_pyarrow, run_rounds


def _block_pyarrow(monkeypatch):
    """Simulate an environment without pyarrow, even when pyarrow is installed."""
    import benchx

    for name in list(sys.modules):
        if name == "pyarrow" or name.startswith("pyarrow.") or name in ("benchx.store", "benchx.parquet"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    for attr in ("store", "parquet"):
        monkeypatch.delattr(benchx, attr, raising=False)
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)


# --- RFC 6901 error-location formatting (replaces jsonpointer) -------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ((), "/"),
        (("source", "uri"), "/source/uri"),
        (("measurement", "observations", 0), "/measurement/observations/0"),
        (("",), "/"),
        (("a", "", "b"), "/a//b"),
        (("", ""), "//"),
        (("a/b", "c~d", "~1/0"), "/a~1b/c~0d/~01~10"),
        (("~", "/"), "/~0/~1"),
    ],
)
def test_error_location_rfc6901_pointer(path, expected):
    err = StructureError(path=path, keyword="type", reason="bad")
    assert err.location == expected
    assert str(err) == f"malformed at {expected}: bad"


# --- Narrowed JSON Schema format checkers & missing-checker enforcement ----------------


@pytest.mark.parametrize(
    "bad_uri",
    [
        "not a uri",
        "relative/path",
        "http://[::1",
    ],
)
def test_invalid_uri_rejected_by_format_checker(adhoc, bad_uri):
    adhoc["source"]["uri"] = bad_uri
    errors = core.check(adhoc, "measurement-result")
    assert len(errors) == 1
    assert isinstance(errors[0], StructureError)
    assert (errors[0].path, errors[0].keyword) == (("source", "uri"), "format")


@pytest.mark.parametrize(
    "bad_date",
    [
        "2026-02-30T10:02:31Z",
        "2026-04-31T12:00:00Z",
        "2026-13-01T00:00:00Z",
    ],
)
def test_impossible_calendar_date_rejected_by_format_checker(adhoc, bad_date):
    # Matches the UTC Z regex pattern, so only the `date-time` format checker catches it.
    adhoc["provenance"]["started_at"] = bad_date
    adhoc["provenance"]["ended_at"] = bad_date
    errors = core.check(adhoc, "measurement-result")
    assert errors
    assert all(isinstance(e, StructureError) and e.keyword == "format" for e in errors)
    assert {e.path for e in errors} == {("provenance", "started_at"), ("provenance", "ended_at")}


def test_valid_leap_and_boundary_dates_accepted(adhoc):
    adhoc["provenance"]["started_at"] = "2024-02-29T23:59:59Z"
    adhoc["provenance"]["ended_at"] = "2024-02-29T23:59:59Z"
    assert core.check(adhoc, "measurement-result") == []


@pytest.mark.parametrize("missing_fmt", ["uri", "date-time"])
def test_missing_required_format_checker_fails_loudly(monkeypatch, missing_fmt):
    """If a required format validator is absent, catalog fails instead of silently skipping."""
    real_checker = FormatChecker()
    remaining = tuple(fmt for fmt in real_checker.checkers if fmt != missing_fmt)
    monkeypatch.setattr(catalog, "FormatChecker", lambda: FormatChecker(formats=remaining))
    catalog.clear_cache()
    try:
        with pytest.raises(catalog.FormatCheckerError, match=missing_fmt):
            catalog.lookup("measurement-result", catalog.MEASUREMENT_RESULT)
        with pytest.raises(catalog.FormatCheckerError, match=missing_fmt):
            catalog.fragment("measurement-result", "0.1.0", "/$defs/uri")
    finally:
        catalog.clear_cache()


def test_custom_benchx_schemas_missing_format_checker_fails(tmp_path, monkeypatch):
    """Custom schemas under $BENCHX_SCHEMAS fail clearly when a declared format has no checker."""
    base_dir = tmp_path / "schemas"
    leaf = base_dir / "custom-leaf" / "0.1.0"
    leaf.mkdir(parents=True)
    (leaf / "schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:benchx:schema:custom-leaf:0.1.0",
                "type": "string",
                "format": "no-such-custom-format",
            }
        )
    )
    root = base_dir / "measurement-result" / "0.1.0"
    root.mkdir(parents=True)
    (root / "schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:benchx:schema:measurement-result:0.1.0",
                "type": "object",
                "properties": {
                    "x": {"$ref": "urn:benchx:schema:custom-leaf:0.1.0"},
                },
            }
        )
    )
    monkeypatch.setattr(catalog, "SCHEMAS", base_dir)
    catalog.clear_cache()
    try:
        with pytest.raises(catalog.FormatCheckerError, match="no-such-custom-format"):
            catalog.lookup("measurement-result", "benchx/measurement-result/0.1.0")
    finally:
        catalog.clear_cache()


# --- Canonical hashes and identities remain unchanged ----------------------------------


def test_canonical_hashes_and_identities_unchanged(adhoc, arrow):
    assert core.sha256_hex(core.canonical(adhoc)) == (
        "a46dd3a0aff71599c81ef0866c4912b6cd68544e8339a60ff7694c5c62aae720"
    )
    assert identity.reported_coordinates_fingerprint(adhoc) == (
        "c6037bbc4eaa0ed27fa54aab68ff1eeb34968cbe15bc89828615223776e0e54d"
    )
    assert identity.series_fingerprint(adhoc, store_project(adhoc), identity.MEDIAN) == (
        "b44af1d5029f2b1a222c19cf1acff434e1cb858086aec5c46093e6f9b9d0cd79"
    )

    assert core.sha256_hex(core.canonical(arrow)) == (
        "821864f4131f18e89226f733da8b0246f4275373fe95a50bdd79efc40994fe0c"
    )
    assert identity.reported_coordinates_fingerprint(arrow) == (
        "0ca627d985be3f63c29e306939a598e29338d63983d8186ebee5d7c8f6492926"
    )
    assert identity.series_fingerprint(arrow, store_project(arrow), identity.MEDIAN) == (
        "fb97ab834dea457cef9af2929daf359512383977e2e811db176899e9f5297221"
    )


# --- Arrow-free CLI imports, execution, and JSON-only comparisons ----------------------


def test_importing_cli_and_non_store_modules_does_not_import_pyarrow():
    code = (
        "import sys\n"
        "import benchx.cli, benchx.bench, benchx.compare, benchx.tables, benchx.results, benchx.runner, benchx.store, benchx.views\n"
        "assert 'pyarrow' not in sys.modules, 'pyarrow was imported unexpectedly'\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_cli_help_without_pyarrow(monkeypatch):
    _block_pyarrow(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_bench_run_and_compare_results_without_pyarrow(repo, tmp, monkeypatch, capsys):
    """End-to-end without pyarrow: bx run --no-ingest, bx bench --no-ingest, bx compare --results."""
    _block_pyarrow(monkeypatch)
    base = make_build(tmp / "build-base", repo["wt-base"], cases())
    head = make_build(tmp / "build-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))

    # 1. bx run --no-ingest
    order_path = tmp / "order.json"
    order_path.write_text(json.dumps(order(base, repo["wt-base"], "run-no-ingest")))
    assert cli.main(["run", str(order_path), "--out", str(tmp / "run-out"), "--no-ingest"]) == cli.EXIT_OK
    assert not (tmp / "store.parquet").exists()
    capsys.readouterr()

    # 2. bx bench --no-ingest across 4 alternating rounds of two sides (split into two dirs)
    out_base = tmp / "out-base"
    out_head = tmp / "out-head"
    slot = 0
    for r in range(4):
        for target, out_dir in (
            (f"{base}:{repo['wt-base']}", out_base),
            (f"{head}:{repo['wt-head']}", out_head),
        ):
            assert (
                cli.main(
                    [
                        "bench",
                        target,
                        "--adapter",
                        "google-benchmark",
                        "--suite",
                        "demo-bench",
                        "--run-key",
                        "rev-no-arrow",
                        "--round",
                        str(r),
                        "--slot",
                        str(slot),
                        "--out",
                        str(out_dir),
                        "--no-ingest",
                    ]
                )
                == cli.EXIT_OK
            )
            slot += 1
    assert not (tmp / "store.parquet").exists()
    capsys.readouterr()

    # 3. bx compare --run ... --results DIR1 DIR2 (JSON and --tables) without pyarrow
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "rev-no-arrow",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--results",
                str(out_base),
                str(out_head),
                "--json",
            ]
        )
        == cli.EXIT_OK
    )
    doc = json.loads(capsys.readouterr().out)
    assert core.check(doc, "comparison-document") == []
    assert doc["failed_invariants"] == [] and doc["missing"] == []
    verdicts = {u["workload"]["name"]: u["verdict"] for u in doc["units"]}
    assert verdicts == {QUIET: "regressed", NOISY: "no-change-detected"}

    assert (
        cli.main(
            [
                "compare",
                "--run",
                "rev-no-arrow",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--results",
                str(out_base),
                str(out_head),
                "--tables",
            ]
        )
        == cli.EXIT_OK
    )
    table_out = capsys.readouterr().out
    assert "REGRESSED" in table_out and "| Change" in table_out
    assert not list(tmp.glob("*.parquet"))


def test_compare_measure_mode_no_ingest_without_pyarrow(repo, tmp, monkeypatch, capsys):
    """`bx compare BASE CONTENDER --no-ingest` runs and compares in memory without pyarrow."""
    _block_pyarrow(monkeypatch)
    base = make_build(tmp / "b-base", repo["wt-base"], cases())
    head = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))
    out = tmp / "measure-out"

    assert (
        cli.main(
            [
                "compare",
                f"{base}:{repo['wt-base']}",
                f"{head}:{repo['wt-head']}",
                "--profile",
                "revisions",
                "--adapter",
                "google-benchmark",
                "--suite",
                "demo-bench",
                "--rounds",
                "4",
                "--run-key",
                "m-no-arrow",
                "--out",
                str(out),
                "--no-ingest",
                "--json",
            ]
        )
        == cli.EXIT_OK
    )
    measured = json.loads(capsys.readouterr().out)
    assert core.check(measured, "comparison-document") == []
    assert measured["failed_invariants"] == [] and not list(tmp.glob("*.parquet"))
    assert {u["workload"]["name"]: u["verdict"] for u in measured["units"]} == {
        QUIET: "regressed",
        NOISY: "no-change-detected",
    }

    # Reading those same JSON files back with --results gives the exact same document.
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "m-no-arrow",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--results",
                str(out),
                "--json",
            ]
        )
        == cli.EXIT_OK
    )
    assert json.loads(capsys.readouterr().out) == measured

    # --no-ingest is rejected in read mode.
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "m-no-arrow",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--no-ingest",
            ]
        )
        == cli.EXIT_REFUSED
    )
    assert "measure mode" in capsys.readouterr().err


def test_compare_variants_measure_mode_no_ingest_without_pyarrow(tmp, monkeypatch, capsys):
    """`bx compare TARGET --profile variants --no-ingest` works without pyarrow."""
    pytest.importorskip("pyperf")
    _block_pyarrow(monkeypatch)
    tree = tmp / "variants-tree"
    tree.mkdir()
    git(tree, "init", "-q")
    (tree / "mod.py").write_text(
        "def bench_work(role):\n"
        "    sum(range(10 if role == 'baseline' else 2000))\n"
        "bench_work.params = ['baseline', 'variant']\n"
        "bench_work.param_names = ['role']\n"
    )
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "init")

    assert (
        cli.main(
            [
                "compare",
                str(tree),
                "--profile",
                "variants",
                "--adapter",
                "pyperf",
                "--suite",
                "mod.py",
                "--rounds",
                "2",
                "--min-rounds",
                "2",
                "--processes",
                "2",
                "--values",
                "2",
                "--loops",
                "50",
                "--warmups",
                "0",
                "--run-key",
                "v-no-arrow",
                "--out",
                str(tmp / "v-out"),
                "--no-ingest",
                "--tables",
                "--all",
            ]
        )
        == cli.EXIT_OK
    )
    out = capsys.readouterr().out
    assert "bench_work" in out and "| Change" in out
    assert not list(tmp.glob("*.parquet"))


def test_default_cli_uses_jsonl_store_without_pyarrow(repo, tmp, monkeypatch, capsys):
    """Without pyarrow, default bx bench/run/ingest/head/series/history/compare all work via store.jsonl."""
    _block_pyarrow(monkeypatch)
    base = make_build(tmp / "b-base", repo["wt-base"], cases())
    head = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))

    # 1. bx bench (default — no --no-ingest!) creates store.jsonl
    assert (
        cli.main(
            [
                "bench",
                f"{base}:{repo['wt-base']}",
                "--adapter",
                "google-benchmark",
                "--suite",
                "demo-bench",
                "--run-key",
                "jsonl-bench",
                "--out",
                str(tmp / "bench-out"),
            ]
        )
        == cli.EXIT_OK
    )
    assert (tmp / "store.jsonl").is_file()
    assert not (tmp / "store.parquet").exists()
    capsys.readouterr()

    # 2. bx run (default — no --no-ingest!) appends to store.jsonl
    order_path = tmp / "order.json"
    order_path.write_text(json.dumps(order(base, repo["wt-base"], "jsonl-run")))
    assert cli.main(["run", str(order_path), "--out", str(tmp / "run-out")]) == cli.EXIT_OK
    capsys.readouterr()

    # 3. bx ingest (idempotent re-ingest)
    assert cli.main(["ingest", str(tmp / "run-out")]) == cli.EXIT_OK
    assert "duplicate" in capsys.readouterr().out

    # 4. bx head, bx head --table, bx series, bx history
    assert cli.main(["head"]) == cli.EXIT_OK
    assert "jsonl-run" in capsys.readouterr().out
    assert cli.main(["head", "--table"]) == cli.EXIT_OK
    assert QUIET in capsys.readouterr().out
    assert cli.main(["series"]) == cli.EXIT_OK
    series_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert series_lines
    fp = series_lines[0].split()[0]
    assert cli.main(["history", fp]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip()

    # 5. bx compare in measure mode (no --no-ingest!) persists to store.jsonl and compares
    assert (
        cli.main(
            [
                "compare",
                f"{base}:{repo['wt-base']}",
                f"{head}:{repo['wt-head']}",
                "--profile",
                "revisions",
                "--adapter",
                "google-benchmark",
                "--suite",
                "demo-bench",
                "--rounds",
                "4",
                "--run-key",
                "jsonl-cmp",
                "--out",
                str(tmp / "cmp-out"),
                "--json",
            ]
        )
        == cli.EXIT_OK
    )
    measured = json.loads(capsys.readouterr().out)
    assert core.check(measured, "comparison-document") == []
    assert {u["workload"]["name"]: u["verdict"] for u in measured["units"]} == {
        QUIET: "regressed",
        NOISY: "no-change-detected",
    }

    # 6. bx compare --run (read mode, no --results!) reads from store.jsonl
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "jsonl-cmp",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--json",
            ]
        )
        == cli.EXIT_OK
    )
    assert json.loads(capsys.readouterr().out) == measured
    assert not list(tmp.glob("*.parquet"))


@pytest.mark.parametrize(
    "argv, extra_hint",
    [
        (["ingest", "results"], None),
        (["series"], None),
        (["history", "deadbeef"], None),
        (["head"], None),
        (["run", "order.json"], "--no-ingest"),
        (["compare", "--run", "r1", "--profile", "revisions", "--baseline", "abc"], "--results"),
        (
            ["compare", "A", "B", "--profile", "revisions", "--adapter", "pyperf", "--suite", "s", "--rounds", "3"],
            "--no-ingest",
        ),
    ],
)
def test_existing_parquet_store_refuses_cleanly_without_pyarrow(tmp, monkeypatch, capsys, argv, extra_hint):
    """When store.parquet already exists (or is explicitly configured), missing pyarrow refuses instead of splitting history."""
    _block_pyarrow(monkeypatch)
    (tmp / "store.parquet").write_bytes(b"PAR1")
    assert cli.main(argv) == cli.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "benchx[parquet]" in err
    assert not (tmp / "store.jsonl").exists()
    if extra_hint:
        assert extra_hint in err


def test_bench_refuses_cleanly_when_parquet_store_exists_without_pyarrow(repo, tmp, monkeypatch, capsys):
    _block_pyarrow(monkeypatch)
    (tmp / "store.parquet").write_bytes(b"PAR1")
    base = make_build(tmp / "b", repo["wt-base"], cases())
    assert (
        cli.main(
            [
                "bench",
                f"{base}:{repo['wt-base']}",
                "--adapter",
                "google-benchmark",
                "--suite",
                "demo-bench",
            ]
        )
        == cli.EXIT_REFUSED
    )
    err = capsys.readouterr().err
    assert "benchx[parquet]" in err and "--no-ingest" in err
    assert not (tmp / "store.jsonl").exists()


@requires_pyarrow
def test_existing_jsonl_store_is_kept_when_pyarrow_is_installed(tmp):
    """Installing pyarrow later does not orphan an existing store.jsonl."""
    from benchx import config as config_mod

    (tmp / "store.jsonl").write_text("")
    assert config_mod.load(tmp).store_path == tmp / "store.jsonl"


def test_direct_parquet_store_and_parquet_import_explain_parquet_extra(tmp, monkeypatch):
    _block_pyarrow(monkeypatch)
    from benchx.store import Store

    # JSONL Store works without pyarrow; Parquet Store and benchx.parquet fail clearly.
    assert Store(tmp / "store.jsonl").head() == (0, [])
    with pytest.raises(ModuleNotFoundError, match=r"benchx\[parquet\]"):
        Store(tmp / "store.parquet")
    with pytest.raises(ModuleNotFoundError, match=r"benchx\[parquet\]"):
        importlib.import_module("benchx.parquet")


# --- Parity between JSON-only (MemoryStore) and Parquet-backed (Store) -----------------


@requires_pyarrow
def test_json_only_and_parquet_store_parity_for_comparisons_and_failed_cases(repo, tmp, capsys):
    from benchx.store import Store

    base = make_build(tmp / "b-base", repo["wt-base"], cases(), error=["BM_Broken"], skip=["BM_Skipped"])
    head = make_build(
        tmp / "b-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3), error=["BM_Broken"], skip=["BM_Skipped"]
    )
    out = run_rounds(
        tmp,
        [{"build": base, "source": repo["wt-base"]}, {"build": head, "source": repo["wt-head"]}],
        rounds=4,
        run_key="parity-1",
        out=tmp / "results-parity",
    )

    store = Store(tmp / "store.parquet")
    assert store.ingest_paths([out])["rejected"] == []

    # Compare JSON documents from Parquet store vs --results (MemoryStore)
    common = ["--run", "parity-1", "--profile", "revisions", "--baseline", repo["base"][:10]]
    assert cli.main(["compare", *common, "--json"]) == cli.EXIT_OK
    from_parquet = json.loads(capsys.readouterr().out)

    assert cli.main(["compare", *common, "--results", str(out), "--json"]) == cli.EXIT_OK
    from_json = json.loads(capsys.readouterr().out)
    assert from_json == from_parquet

    # Compare --tables text output too
    assert cli.main(["compare", *common, "--tables", "--all"]) == cli.EXIT_OK
    tables_parquet = capsys.readouterr().out
    assert cli.main(["compare", *common, "--results", str(out), "--tables", "--all"]) == cli.EXIT_OK
    tables_json = capsys.readouterr().out
    assert tables_json == tables_parquet


@requires_pyarrow
def test_json_only_and_parquet_store_parity_for_ingest_contract(repo, tmp, adhoc, capsys):
    from benchx.store import Store

    base = make_build(tmp / "b-base", repo["wt-base"], cases())
    head = make_build(tmp / "b-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))
    out = run_rounds(
        tmp,
        [{"build": base, "source": repo["wt-base"]}, {"build": head, "source": repo["wt-head"]}],
        rounds=3,
        run_key="contract-1",
        out=tmp / "results-contract",
    )

    parquet_store = Store(tmp / "store.parquet")
    jsonl_store = Store(tmp / "store.jsonl")
    memory_store = MemoryStore()

    # Initial ingest and duplicate re-ingest
    assert memory_store.ingest_paths([out]) == parquet_store.ingest_paths([out]) == jsonl_store.ingest_paths([out])
    assert memory_store.ingest_paths([out]) == parquet_store.ingest_paths([out]) == jsonl_store.ingest_paths([out])
    assert (
        memory_store.documents("contract-1")
        == parquet_store.documents("contract-1")
        == jsonl_store.documents("contract-1")
    )
    assert jsonl_store.head(n=50) == parquet_store.head(n=50)
    assert jsonl_store.head(n=2, workload=QUIET, measurands=True) == parquet_store.head(
        n=2, workload=QUIET, measurands=True
    )
    assert jsonl_store.entities() == parquet_store.entities()
    assert jsonl_store.series() == parquet_store.series()
    fp = parquet_store.series()[0]["fingerprint"]
    assert jsonl_store.history(fp) == parquet_store.history(fp)

    doc = memory_store.documents("contract-1")[0]
    ref = doc["provenance"]["info"]["workorder_ref"]
    assert memory_store.order(ref) == parquet_store.order(ref) == jsonl_store.order(ref)

    # Every conflict and malformed rejection code matches identically
    cases_to_check = []

    unit_conflict = json.loads(json.dumps(doc))
    unit_conflict["ingest_key"] += ":ms"
    unit_conflict["measurement"]["quantity"]["unit"] = "ms"
    cases_to_check.append(("unit.json", json.dumps(unit_conflict)))

    attempt_conflict = json.loads(json.dumps(doc))
    attempt_conflict["ingest_key"] += ":moved"
    attempt_conflict["coordinates"]["workload"]["name"] = "other"
    cases_to_check.append(("attempt.json", json.dumps(attempt_conflict)))

    identity_violation = json.loads(json.dumps(doc))
    identity_violation["ingest_key"] += ":src"
    identity_violation["coordinates"]["subject"]["components"][0]["source"] = "https://elsewhere.org/x"
    cases_to_check.append(("identity.json", json.dumps(identity_violation)))

    idempotency_conflict = json.loads(json.dumps(doc))
    idempotency_conflict["measurement"]["observations"][0] *= 2
    cases_to_check.append(("idem.json", json.dumps(idempotency_conflict)))

    nan_text = json.dumps(doc).replace('"observations": [', '"observations": [NaN, ', 1)
    cases_to_check.append(("nan.json", nan_text))

    dup_key_text = json.dumps(doc)[:-1] + ', "ingest_key": "other"}'
    cases_to_check.append(("dupkey.json", dup_key_text))

    assert (
        memory_store.ingest(cases_to_check)
        == parquet_store.ingest(cases_to_check)
        == jsonl_store.ingest(cases_to_check)
    )

    # Unreadable files and bad workorders in a directory sweep
    bad_dir = tmp / "bad-sweep"
    bad_dir.mkdir()
    (bad_dir / "good.json").write_text(json.dumps(adhoc))
    (bad_dir / "invalid-utf8.json").write_bytes(b"\xff\xfe\x00")
    (bad_dir / "workorder-bad.json").write_text("{broken")
    missing = tmp / "does-not-exist.json"
    assert (
        memory_store.ingest_paths([bad_dir, missing])
        == parquet_store.ingest_paths([bad_dir, missing])
        == jsonl_store.ingest_paths([bad_dir, missing])
    )

    # CLI `--results` reports rejections identically and exits EXIT_INVALID
    assert (
        cli.main(
            [
                "compare",
                "--run",
                "contract-1",
                "--profile",
                "revisions",
                "--baseline",
                repo["base"][:10],
                "--results",
                str(bad_dir),
            ]
        )
        == cli.EXIT_INVALID
    )
    err = capsys.readouterr().err
    assert "rejected invalid-utf8.json: malformed" in err
    assert "rejected workorder-bad.json: malformed" in err
