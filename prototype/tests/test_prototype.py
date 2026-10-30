"""The success criteria of prototype-scope.md §3, on fake Google Benchmark output."""

import json
import re
import time
from pathlib import Path

import pytest

from benchx import compare, core, identity, runner
from benchx.core.errors import StructureError
from benchx.store import Store
from conftest import NOISY, QUIET, SOURCE_URI, cases, git, make_build, order, run_rounds


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def results(directory: Path, conditions=False) -> list[dict]:
    """The result files in `directory`: the measurands, and with `conditions`
    also the load-average sibling the runner adds to every attempt."""
    docs = [load(p) for p in sorted(directory.glob("*.json")) if not p.name.startswith("workorder-")]
    return [d for d in docs if conditions or d["measurement"].get("kind") != "condition"]


def outcome(doc) -> dict:
    """A measurement without its quantity and kind: the status and what came with it."""
    return {k: v for k, v in doc["measurement"].items() if k not in ("quantity", "kind")}


def verdicts(doc) -> dict:
    return {u["workload"]["name"]: u["verdict"] for u in doc["units"]}


@pytest.fixture
def revisions_run(repo, tmp):
    base = make_build(tmp / "build-base", repo["wt-base"], cases())
    head = make_build(tmp / "build-head", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3))
    out = run_rounds(tmp, [{"build": base, "source": repo["wt-base"]}, {"build": head, "source": repo["wt-head"]}],
                     rounds=6, run_key="rev-1", out=tmp / "results-rev")
    return out


@pytest.fixture
def environments_run(repo, tmp):
    plain = make_build(tmp / "build-plain", repo["wt-head"], cases())
    hardened = make_build(tmp / "build-hardened", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3),
                          hardened=True)
    sides = [{"build": plain, "source": repo["wt-head"], "labels": {"build": "plain"}},
             {"build": hardened, "source": repo["wt-head"], "labels": {"build": "hardened"}}]
    return run_rounds(tmp, sides, rounds=6, run_key="env-1", out=tmp / "results-env")


def test_results_are_valid_documents(revisions_run):
    docs = results(revisions_run)
    assert len(docs) == 2 * 6 * 2  # cases × rounds × sides
    every = results(revisions_run, conditions=True)
    assert len(every) == 2 * len(docs)  # each attempt's load average beside it (schema §4.4)
    for doc in every:
        assert core.check(doc, "measurement-result") == []  # criterion 1: the file is the ingest payload
        assert doc["provenance"]["subject_dirty"] == "clean"
        assert doc["coordinates"]["environment"]["schema_version"] == "machine/v1"
        config = doc["coordinates"]["subject"]["configuration"]
        assert config["options"] == {"DEMO_HARDENED": "OFF"}  # DEMO_DATA_DIR is a path
        assert config["cxx_compiler"] == "AppleClang 21.0.0"
    attempts = {d["attempt_key"] for d in docs}
    for load_ in (d for d in every if d not in docs):
        assert load_["measurement"]["quantity"] == {"name": "load-average", "unit": "1"}
        assert load_["coordinates"]["protocol"] == {"probe": runner.LOAD_PROBE}
        assert load_["measurement"]["kind"] == "condition"
        assert load_["attempt_key"] in attempts


def test_revisions_profile(revisions_run, repo, tmp):
    store = Store(tmp / "store.parquet")
    assert store.ingest_paths([revisions_run])["ingested"] == 48  # with the load averages
    doc = compare.compare(store.documents("rev-1"), run_key="rev-1", profile="revisions",
                          baseline=repo["base"][:10])
    assert core.check(doc, "comparison-document") == []
    assert doc["failed_invariants"] == [] and doc["missing"] == []
    # criterion 5: +2% on a quiet benchmark flags, +3% on a noisy one does not
    assert verdicts(doc) == {QUIET: "regressed", NOISY: "no-change-detected"}
    assert doc["local_only"] is False
    assert all(u["rounds"] == 6 for u in doc["units"])


def test_environments_profile(environments_run, tmp):
    store = Store(tmp / "store.parquet")
    store.ingest_paths([environments_run])
    doc = compare.compare(store.documents("env-1"), run_key="env-1", profile="environments", label="build",
                          baseline="plain")
    assert doc["varying"] == "provenance.labels.build"
    assert verdicts(doc) == {QUIET: "regressed", NOISY: "no-change-detected"}


def test_environments_needs_a_real_difference(repo, tmp):
    a = make_build(tmp / "a", repo["wt-head"], cases())
    b = make_build(tmp / "b", repo["wt-head"], cases())
    out = run_rounds(tmp, [{"build": a, "source": repo["wt-head"], "labels": {"build": "a"}},
                           {"build": b, "source": repo["wt-head"], "labels": {"build": "b"}}],
                     rounds=3, run_key="same", out=tmp / "same")
    doc = compare.compare(results(out), run_key="same", profile="environments", label="build", baseline="a")
    assert core.check(doc, "comparison-document") == []
    assert [f["invariant"] for f in doc["failed_invariants"]] == ["sides-differ"]
    assert doc["units"] == []


def test_ingest_contract(revisions_run, tmp):
    store = Store(tmp / "store.parquet")
    assert store.ingest_paths([revisions_run]) == {"ingested": 48, "duplicate": 0, "rejected": []}
    # criterion 2: re-ingest is a visible no-op
    assert store.ingest_paths([revisions_run]) == {"ingested": 0, "duplicate": 48, "rejected": []}

    doc = results(revisions_run)[0]

    def attempt(mutate, key_suffix):
        changed = json.loads(json.dumps(doc))
        changed["ingest_key"] += key_suffix
        mutate(changed)
        return store.ingest([("x.json", json.dumps(changed))])["rejected"][0]["code"]

    # criterion 3: a conflicting unit is rejected with its taxonomy code
    assert attempt(lambda d: d["measurement"]["quantity"].update(unit="ms"), ":ms") == "unit-conflict"
    assert attempt(lambda d: d["coordinates"]["workload"].update(name="other"), ":moved") == "attempt-conflict"
    assert attempt(lambda d: d["coordinates"]["subject"]["components"][0].update(
        source="https://elsewhere.org/x"), ":src") == "identity-violation"
    same_key = json.loads(json.dumps(doc))
    same_key["measurement"]["observations"][0] *= 2
    assert store.ingest([("y.json", json.dumps(same_key))])["rejected"][0]["code"] == "idempotency-conflict"
    text = json.dumps(doc).replace('"observations": [', '"observations": [NaN, ', 1)
    assert store.ingest([("z.json", text)])["rejected"][0]["code"] == "malformed"


def test_same_document_from_any_store(revisions_run, repo, tmp):
    """Criterion 8: a throwaway store and a persistent one give one document."""
    documents = []
    for name in ("persistent", "throwaway"):
        store = Store(tmp / f"{name}.parquet")
        store.ingest_paths([revisions_run])
        documents.append(compare.compare(store.documents("rev-1"), run_key="rev-1", profile="revisions",
                                         baseline=repo["base"]))
    assert documents[0] == documents[1]


def test_traceability(revisions_run, tmp):
    """Criterion 6: result → order, verdict → results."""
    store = Store(tmp / "store.parquet")
    store.ingest_paths([revisions_run])
    docs = store.documents("rev-1")
    for doc in docs:
        assert store.order(doc["provenance"]["info"]["workorder_ref"])["run_key"] == "rev-1"
    comparison = compare.compare(docs, run_key="rev-1", profile="revisions", baseline=docs[0]["revision"]["key"])
    measured = [d for d in docs if d["measurement"]["quantity"]["name"] != "load-average"]
    assert {(i["producer"], i["ingest_key"]) for i in comparison["inputs"]} == \
        {(d["producer"]["name"], d["ingest_key"]) for d in measured}


def test_missing_and_unequal(repo, tmp):
    """Criterion 4: a variant on one side only is reported, never dropped."""
    base = make_build(tmp / "b1", repo["wt-base"], cases())
    head = make_build(tmp / "b2", repo["wt-head"], {QUIET: cases()[QUIET]})
    out = run_rounds(tmp, [{"build": base, "source": repo["wt-base"]}, {"build": head, "source": repo["wt-head"]}],
                     rounds=3, run_key="miss", out=tmp / "miss")
    doc = compare.compare(results(out), run_key="miss", profile="revisions", baseline=repo["base"])
    assert doc["missing"] == [{"workload": {"name": NOISY}, "quantity": "wall-time", "side": "contender"}]
    assert QUIET in verdicts(doc)


def test_alternation_is_checked(repo, tmp):
    base = make_build(tmp / "b1", repo["wt-base"], cases())
    head = make_build(tmp / "b2", repo["wt-head"], cases())
    for slot, (build, source) in enumerate([(base, "wt-base"), (base, "wt-base"), (head, "wt-head"),
                                            (head, "wt-head")]):
        path = tmp / f"o{slot}.json"
        path.write_text(json.dumps(order(build, repo[source], "seq", round=slot % 2, slot=slot)))
        runner.run(path, tmp / "seq")
    doc = compare.compare(results(tmp / "seq"), run_key="seq", profile="revisions", baseline=repo["base"])
    assert [f["invariant"] for f in doc["failed_invariants"]] == ["alternating-slots"]


def _dirty_run(repo, tmp, profile_sides, run_key):
    (repo["wt-head"] / "bench.cpp").write_text("// work = 103, uncommitted\n")
    base = make_build(tmp / "b1", repo["wt-base"], cases())
    head = make_build(tmp / "b2", repo["wt-head"], cases())
    return run_rounds(tmp, profile_sides(base, head), rounds=3, run_key=run_key, out=tmp / run_key)


def test_ad_hoc_is_thin_and_a_dirty_run_is_accepted(repo, tmp):
    """Criterion 7: no project, no configuration; a dirty single run is recorded as dirty (UC-01)."""
    out = _dirty_run(repo, tmp, lambda base, head: [{"build": base, "source": repo["wt-base"]},
                                                     {"build": head, "source": repo["wt-head"]}], "adhoc")
    docs = results(out)
    assert all("project" not in d for d in docs)
    assert {d["provenance"]["subject_dirty"] for d in docs} == {"clean", "dirty"}
    store = Store(tmp / "store.parquet")
    assert store.ingest_paths([out])["rejected"] == []
    assert {s["project"] for s in store.series()} == {f"local/{docs[0]['coordinates']['environment']['identity']['runner']}"}


def test_revisions_refuses_a_dirty_side(repo, tmp):
    """Criterion 7, UC-03 §10: a revisions comparison with a dirty side gets no verdicts."""
    out = _dirty_run(repo, tmp, lambda base, head: [{"build": base, "source": repo["wt-base"]},
                                                     {"build": head, "source": repo["wt-head"]}], "dirty-rev")
    store = Store(tmp / "store.parquet")
    store.ingest_paths([out])
    doc = compare.compare(store.documents("dirty-rev"), run_key="dirty-rev", profile="revisions",
                          baseline=repo["base"])
    assert [f["invariant"] for f in doc["failed_invariants"]] == ["clean-tree"]
    assert doc["units"] == []


def test_environments_labels_a_dirty_side_local_only(repo, tmp):
    """Criterion 7, schema §5.5: the environments profile still allows a dirty side, labeled."""
    out = _dirty_run(repo, tmp, lambda base, head: [
        {"build": base, "source": repo["wt-head"], "labels": {"build": "a"}},
        {"build": make_build(tmp / "b3", repo["wt-head"], cases(quiet=1.02e-3), hardened=True),
         "source": repo["wt-head"], "labels": {"build": "b"}}], "dirty-env")
    store = Store(tmp / "store.parquet")
    store.ingest_paths([out])
    doc = compare.compare(store.documents("dirty-env"), run_key="dirty-env", profile="environments",
                          label="build", baseline="a")
    assert doc["failed_invariants"] == [] and doc["local_only"] is True
    assert doc["units"]


def test_tracked_series_and_history(revisions_run, tmp):
    store = Store(tmp / "store.parquet")
    store.ingest_paths([revisions_run])
    series = store.series(workload=QUIET, quantity="wall-time")
    medians = [s for s in series if json.loads(s["estimator"]) == identity.MEDIAN]
    # the benchmark revision is ignored by the identity policy, so both revisions share a series
    assert len(medians) == 1 and medians[0]["identity_schema"] == "benchx/prototype-identity/v1"
    # Google Benchmark's own median is a different estimator, so a different series
    assert {json.loads(s["estimator"])["method_version"] for s in series} == {
        "benchx/median/v1", "google-benchmark/median", "google-benchmark/mean"}
    points = store.history(medians[0]["fingerprint"][:12])
    assert len(points) == 12
    # an ignored fact that changes along the series is flagged on the point (schema §4.3)
    changes = [p["changed"] for p in points]
    assert changes[0] == [] and ["/benchmark/revision"] in changes


def test_another_projection_on_request(revisions_run, tmp):
    """Schema §4.3: the same results under the revisions profile's projection, one series per tree."""
    store = Store(tmp / "store.parquet")
    store.ingest_paths([revisions_run])
    profile = compare.PROFILES["revisions"]
    medians = [s for s in store.series(workload=QUIET, quantity="wall-time", projection=profile)
               if json.loads(s["estimator"]) == identity.MEDIAN]
    assert len(medians) == 1 and medians[0]["identity_schema"] == profile.identity_schema
    points = store.history(medians[0]["fingerprint"], profile)
    assert len(points) == 12 and len({tuple(p["axis"]) for p in points}) == 2


def test_failures_are_results(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases(), error=["BM_Broken"], skip=["BM_Skipped"],
                       sleep={"BM_Slow": 3})
    path = tmp / "o.json"
    path.write_text(json.dumps(order(build, repo["wt-head"], "fail", timeout_seconds=1)))
    runner.run(path, tmp / "fail")
    by_case = {d["coordinates"]["workload"]["name"]: d for d in results(tmp / "fail")}
    assert outcome(by_case["BM_Broken"]) == {"status": "error", "reason": "harness.error"}
    assert outcome(by_case["BM_Skipped"]) == {"status": "skipped", "reason": "harness.skipped"}
    assert outcome(by_case["BM_Slow"]) == {"status": "error", "reason": "timeout"}
    assert by_case["BM_Slow"]["procedure"]["timeout_seconds"] == 1


@pytest.mark.parametrize("change, message", [
    (lambda o: o["precision"].update(warmup={"mode": "count", "n_warmup": 2}), "warmup"),
    (lambda o: o.update(target={"kind": "artifact", "uri": "file:///tmp/x", "sha256": "0" * 64,
                                "source": {"uri": SOURCE_URI, "type": "git"}, "revision": "abc"}),
     "build and working_tree targets"),
    (lambda o: o.update(target={"kind": "revision", "source": {"uri": SOURCE_URI, "type": "git"},
                                "revision": "deadbeef"}), "malformed at /target"),  # the runner never builds
    (lambda o: o.update(environment_policy={"name": "laptop-default", "version": "1.0.0"}),
     "environment_policy is deferred"),
    (lambda o: o.update(build={"type": "release"}), "build is deferred"),
    (lambda o: o.update(components=[{"name": "numpy", "role": "dependency", "revision": "abc",
                                     "source": {"uri": SOURCE_URI, "type": "git"}}]),
     "components is deferred"),
    (lambda o: o.update(schedule={"kind": "alternating"}), "schedule is deferred"),
    (lambda o: o["suites"][0].update(suite="missing-binary"), "not found"),
    (lambda o: o.update(quantities=["peak-rss"]), "quantities"),
    (lambda o: o.update(surprise=1), "'surprise' was unexpected"),
])
def test_refusals(repo, tmp, change, message):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    document = order(build, repo["wt-head"], "refused")
    change(document)
    path = tmp / "o.json"
    path.write_text(json.dumps(document))
    with pytest.raises(runner.Refused, match=message):
        runner.run(path, tmp / "refused")
    assert not list((tmp / "refused").glob("*.json")) if (tmp / "refused").exists() else True


def refused(tmp, document) -> list:
    path = tmp / "o.json"
    path.write_text(json.dumps(document))
    with pytest.raises(runner.Refused) as raised:
        runner.run(path, tmp / "refused")
    return list(raised.value.problems)


def test_every_refusal_is_reported(repo, tmp):
    document = order(make_build(tmp / "b", repo["wt-head"], cases()), repo["wt-head"], "refused")
    document.update(quantities=["peak-rss"], workload_parameters={"size": 1}, schedule={"kind": "alternating"})
    document["precision"].update(warmup={"mode": "count", "n_warmup": 2})
    assert [str(problem) for problem in refused(tmp, document)] == [
        "google-benchmark takes no workload_parameters",
        "quantities not supported: ['peak-rss']",
        "schedule is deferred in the prototype: side scheduling (the session loop alternates the sides)",
        "google-benchmark warmup is time-based only, not count-based",
    ]


@pytest.mark.parametrize("script, message", [
    ("#!/bin/sh\necho 'cannot open the dataset' >&2\nexit 3\n",
     "demo-bench failed to list its benchmarks: exit status 3: cannot open the dataset"),
    ("#!/bin/sh\nexit 1\n", "demo-bench failed to list its benchmarks: exit status 1: no message"),
    ("\x00\x01 not a program", "cannot run demo-bench to list its benchmarks: "),
], ids=["exits-3-with-a-message", "exits-1-silently", "cannot-start"])
def test_a_binary_that_cannot_list_its_benchmarks_is_a_refusal(repo, tmp, capsys, script, message):
    """The planned cases come from asking the binary, so one that fails or cannot start cannot be
    planned: the order is refused with what it said, before any case runs, not with a traceback."""
    from benchx import cli
    build = make_build(tmp / "b", repo["wt-head"], cases())
    (build / "demo-bench").write_bytes(script.encode())
    path = tmp / "o.json"
    path.write_text(json.dumps(order(build, repo["wt-head"], "nolist")))
    with pytest.raises(runner.Refused, match=re.escape(message)):
        runner.run(path, tmp / "out")
    assert [p for p in (tmp / "out").glob("*.json") if not p.name.startswith("workorder-")] == []
    assert cli.main(["run", str(path), "--out", str(tmp / "out")]) == cli.EXIT_REFUSED
    assert f"refused: {message}" in capsys.readouterr().err


def test_missing_build_is_refused_for_every_reason(tmp):
    problems = refused(tmp, order(tmp / "absent", None, "refused"))
    assert all(type(problem) is core.Refusal for problem in problems)
    assert [str(problem).split(":")[0] for problem in problems] == [
        "suite binary not found",
        "cannot locate the checkout the target was built from; set target.source_dir",
    ]


def test_invalid_order_is_refused_with_its_document_errors(repo, tmp):
    document = order(make_build(tmp / "b", repo["wt-head"], cases()), repo["wt-head"], "refused")
    document.update(surprise=1, run_key="")
    problems = refused(tmp, document)
    assert {type(problem) for problem in problems} == {StructureError}
    assert {problem.path for problem in problems} == {(), ("run_key",)}

def test_source_found_through_cmake_cache(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    path = tmp / "o.json"
    path.write_text(json.dumps(order(build, None, "cache")))
    runner.run(path, tmp / "cache")
    assert {d["revision"]["key"] for d in results(tmp / "cache")} == {repo["head"]}


def run_one(repo, tmp, build_config=None, **order_extra) -> list[dict]:
    build = make_build(tmp / "one", repo["wt-head"], cases(), **(build_config or {}))
    path = tmp / "one.json"
    path.write_text(json.dumps(order(build, repo["wt-head"], "one", **order_extra)))
    runner.run(path, tmp / "one-out")
    return results(tmp / "one-out")


def test_wall_clock_stepping_back_mid_case(repo, tmp, monkeypatch):
    """A wall clock that steps backwards (NTP, a WSL2 resync) leaves durations non-negative."""
    wall = iter(range(2_000_000_000, 0, -1))
    monkeypatch.setattr(time, "time", lambda: float(next(wall)))
    for doc in run_one(repo, tmp):
        assert doc["procedure"]["duration_seconds"] >= 0
        assert doc["provenance"]["ended_at"] >= doc["provenance"]["started_at"]


def test_planned_case_never_reported(repo, tmp):
    """R1: a case the binary lists but never reports is an error result, not an absence."""
    docs = run_one(repo, tmp, build_config={"ghost": ["BM_Ghost"]})
    ghost = [d for d in docs if d["coordinates"]["workload"]["name"] == "BM_Ghost"]
    assert [outcome(d) for d in ghost] == [{"status": "error", "reason": "harness.no-output"}]


def test_defaults_are_recorded(repo, tmp):
    """R3: settings the order leaves out are recorded as applied."""
    doc = run_one(repo, tmp)[0]
    protocol = doc["coordinates"]["protocol"]
    assert protocol["harness"]["name"] == "google-benchmark"
    assert protocol["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.5}
    assert protocol["warmup"] == {"mode": "none"}


def test_snapshot_and_environment_variables(repo, tmp):
    """R9: the zero-configuration snapshot, and an ordered variable as a parameter."""
    doc = run_one(repo, tmp, environment_variables={"OMP_NUM_THREADS": "1"})[0]
    system = doc["coordinates"]["environment"]["system"]
    assert {"family", "kernel"} <= set(system["os"]) and "architecture" in system
    assert system["process"]["environment"] == {"OMP_NUM_THREADS": "1"}
    assert doc["coordinates"]["workload"]["parameters"] == {"OMP_NUM_THREADS": "1"}


def test_setup_file(repo, tmp, monkeypatch):
    """R10: declared facts land in environment.system.setup; a broken file warns."""
    setup = tmp / "setup.json"
    setup.write_text(json.dumps({"blas": "openblas-0.3.27"}))
    monkeypatch.setenv("BENCHX_SETUP_FILE", str(setup))
    assert run_one(repo, tmp)[0]["coordinates"]["environment"]["system"]["setup"] == {"blas": "openblas-0.3.27"}
    setup.write_text("{not json")
    (tmp / "broken").mkdir()
    doc = run_one(repo, tmp / "broken")[0]
    assert doc["quality"]["warnings"] == ["collector-failed"]
    assert "setup" not in doc["coordinates"]["environment"]["system"]


def test_artifacts(repo, tmp):
    """R12: stdout, stderr, and the native output are kept with checksums."""
    doc = run_one(repo, tmp)[0]
    artifacts = {a["kind"]: a for a in doc["provenance"]["artifacts"]}
    assert set(artifacts) == {"native-output", "stdout", "stderr"}
    native = Path(artifacts["native-output"]["uri"].removeprefix("file://"))
    assert core.sha256_hex(native.read_bytes()) == artifacts["native-output"]["sha256"]


def test_duplicate_keys_are_malformed(revisions_run, tmp):
    """S1: the one check only the strict loader makes."""
    text = json.dumps(results(revisions_run)[0])
    duplicated = text[:-1] + ', "ingest_key": "other"}'
    outcome = Store(tmp / "store.parquet").ingest([("dup.json", duplicated)])
    assert outcome["rejected"][0]["code"] == "malformed" and "duplicate" in outcome["rejected"][0]["message"]


def test_entities_auto_create(revisions_run, tmp):
    """S6: first sight creates the core rows of schema §4.1 (locked decision),
    derived from the one Parquet file that is the whole store."""
    store = Store(tmp / "store.parquet")
    store.ingest_paths([revisions_run])
    assert [p.name for p in tmp.glob("store*")] == ["store.parquet"]
    entities = store.entities()
    assert set(entities) == {"project", "source", "workload_variant", "quantity", "environment", "revision",
                             "series", "series_point"}
    assert len(entities["revision"]) == 2


def test_a_file_that_cannot_be_read_is_one_rejection_not_a_crash(adhoc, tmp, capsys):
    """Schema §5.4: a batch is N independent documents with N outcomes. A file that is
    not UTF-8, or is not there, is rejected as malformed, and the rest is ingested."""
    from benchx import cli
    sweep = tmp / "sweep"
    sweep.mkdir()
    (sweep / "good.json").write_text(json.dumps(adhoc))
    (sweep / "garbage.json").write_bytes(b"\xff\xfe\x00not text")
    (sweep / "workorder-garbage.json").write_bytes(b"\xff")
    missing = tmp / "missing.json"
    store = Store(tmp / "store.parquet")
    outcome = store.ingest_paths([sweep, missing])
    assert (outcome["ingested"], outcome["duplicate"]) == (1, 0)
    rejected = {r["document"]: r for r in outcome["rejected"]}
    assert set(rejected) == {"garbage.json", "workorder-garbage.json", "missing.json"}
    assert {r["code"] for r in rejected.values()} == {"malformed"}
    assert "invalid UTF-8 at byte 0" in rejected["garbage.json"]["message"]
    assert "cannot read file" in rejected["missing.json"]["message"]
    assert len(store.documents(adhoc["provenance"]["run_key"])) == 1

    assert cli.main(["ingest", str(sweep), str(missing)]) == cli.EXIT_INVALID
    captured = capsys.readouterr()
    assert "rejected missing.json: malformed" in captured.err
    assert "ingested 1, duplicate 0, rejected 3 (malformed)" in captured.out  # into the CLI's own store


def test_reads_files_from_an_older_schema(revisions_run, tmp):
    """A store written before a column existed takes new rows; old ones read the column as null."""
    import pyarrow.parquet as pq
    files = sorted(p for p in revisions_run.glob("*.json") if not p.name.startswith("workorder-"))
    store = Store(tmp / "store.parquet")
    store.ingest_paths(files[:10])
    pq.write_table(pq.read_table(store.path).drop_columns(["quality"]), store.path)  # as if older
    assert store.ingest_paths(files[10:])["ingested"] == 38
    assert pq.read_table(store.path).column("quality").null_count >= 10
    assert store.ingest_paths(files)["duplicate"] == 48
    assert len(store.documents("rev-1")) == 48


def test_store_keeps_documents_exactly(revisions_run, tmp):
    """The document column is the canonical document, so nothing outside the file is needed."""
    store = Store(tmp / "store.parquet")
    store.ingest_paths([revisions_run])
    stored = {d["ingest_key"]: d for d in store.documents("rev-1")}
    for doc in results(revisions_run, conditions=True):
        assert core.canonical(stored[doc["ingest_key"]]) == core.canonical(doc)


def test_head(revisions_run, tmp):
    """bx head: the latest rows, newest first."""
    store = Store(tmp / "store.parquet")
    assert store.head() == (0, [])
    store.ingest_paths([revisions_run])
    total, rows = store.head(3)
    assert total == 48 and [r["row"] for r in rows] == [47, 46, 45]
    assert all(r["run_key"] == "rev-1" and r["status"] == "success" and r["median"] >= 0 for r in rows)
    ingested_last = sorted(p for p in revisions_run.glob("*.json") if not p.name.startswith("workorder-"))[-1]
    assert json.loads(rows[0]["document"]) == load(ingested_last)


def test_bx_run_delivers_to_the_local_store(repo, tmp, capsys):
    """bx run writes result files and ingests exactly those into the one local store."""
    from benchx import cli
    from benchx.store import default_path
    build = make_build(tmp / "b", repo["wt-head"], cases())
    for slot, name in enumerate(("o1", "o2")):
        (tmp / f"{name}.json").write_text(json.dumps(order(build, repo["wt-head"], "cli", slot=slot)))
    assert default_path() == tmp / "benchx-home" / "store.parquet"
    assert cli.main(["run", str(tmp / "o1.json"), "--out", str(tmp / "out")]) == cli.EXIT_OK
    assert "ingested 4, duplicate 0" in capsys.readouterr().out  # this run's files only, with load averages
    assert cli.main(["run", str(tmp / "o2.json"), "--out", str(tmp / "out"), "--no-ingest"]) == cli.EXIT_OK
    assert len(results(tmp / "out")) == 4  # both runs' files are written
    total, rows = Store().head()
    assert total == 4 and {load(tmp / "o1.json")["run_key"]} == {r["run_key"] for r in rows}
    ref = json.loads(rows[0]["document"])["provenance"]["info"]["workorder_ref"]
    assert Store().order(ref) == load(tmp / "o1.json")  # the order travelled with its results


def _working_tree_order(path, run_key, **target):
    document = order(path, None, run_key)
    document["target"] = {"kind": "working_tree", "path": str(path),
                          "source": {"uri": SOURCE_URI, "type": "git"}, **target}
    return document


def _run_working_tree(tmp, document, name):
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(document))
    runner.run(path, tmp / name)
    return results(tmp / name)


def test_working_tree_records_git_identity_dirty_and_untouched(repo, tmp):
    """A working_tree target is measured as found: HEAD, dirty, and a tree no commit has."""
    checkout = repo["wt-head"]
    make_build(checkout / "bin", checkout, cases())  # untracked files make the tree dirty
    identity_before = runner.snapshot.git_identity(checkout)
    docs = _run_working_tree(tmp, _working_tree_order(checkout / "bin", "wt-git", source_dir=str(checkout)), "wt-git")
    assert docs
    for doc in docs:
        assert core.check(doc, "measurement-result") == []
        assert doc["revision"]["key"] == repo["head"]
        assert doc["provenance"]["subject_dirty"] == "dirty"
        assert doc["provenance"]["subject_tree"] == identity_before["tree"] != repo["head"]
        assert "configuration" not in doc["coordinates"]["subject"]
    # Never cleaned or checked out; the fake benchmark's own counter file is the only change.
    assert runner.snapshot.git_identity(checkout)["revision"] == repo["head"]
    assert (checkout / "bin" / "demo-bench").exists()


def test_working_tree_defaults_to_the_target_path_for_identity(repo, tmp):
    checkout = repo["wt-head"]
    make_build(checkout / "bin", checkout, cases())
    docs = _run_working_tree(tmp, _working_tree_order(checkout / "bin", "wt-default"), "wt-default")
    assert {d["revision"]["key"] for d in docs} == {repo["head"]}


def test_working_tree_outside_git_falls_back_to_a_directory_hash(tmp):
    plain = tmp / "plain"
    plain.mkdir()
    (plain / "mod.py").write_text("x = 1\n")
    make_build(plain / "bin", plain, cases())
    tree = runner.snapshot.directory_identity(plain)["tree"]  # taken before the run, as the runner does
    docs = _run_working_tree(tmp, _working_tree_order(plain / "bin", "wt-dir", source_dir=str(plain)), "wt-dir")
    for doc in docs:
        assert core.check(doc, "measurement-result") == []
        assert doc["provenance"]["subject_dirty"] == "dirty"
        assert doc["provenance"]["subject_tree"] == tree
        assert doc["revision"]["key"] == f"directory-{tree}"
    (plain / "mod.py").write_text("x = 2\n")
    assert runner.snapshot.directory_identity(plain)["tree"] != tree  # content, not time, identifies it


def test_git_identity_ignores_pycache_folders(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q")
    (src / "mod.py").write_text("x = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "one")
    before = runner.snapshot.git_identity(src)
    for folder in (src / "__pycache__", src / "pkg" / "sub" / "__pycache__"):
        folder.mkdir(parents=True)
        (folder / "mod.cpython-312.pyc").write_bytes(b"\0")
    assert runner.snapshot.git_identity(src) == before
    assert before["dirty"] == "clean"
