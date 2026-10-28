"""The pyperf adapter (harness-adapter.md §6.8), against real pyperf where it matters.

Fast tests translate captured native output; the end-to-end ones run pyperf 2
processes × 2 values with a fixed loop count, so they take about a second each.
"""

import copy
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("pyperf")

from benchx import core, runner, snapshot
from benchx.adapters import pyperf as adapter
from conftest import SOURCE_URI, order, warnings_of

FIXTURES = Path(__file__).parent / "fixtures"
PRECISION = {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 2}, {"unit": "value", "n": 2}]},
             "calibration": {"mode": "fixed", "n_iterations": 50}, "warmup": {"mode": "count", "n_warmup": 1}}
MODULE = textwrap.dedent('''
    import os, time

    def f():
        sum(range(100))

    def needs_openblas():
        if "OPENBLAS_NUM_THREADS" not in os.environ:
            raise RuntimeError("OPENBLAS_NUM_THREADS is missing")

    def boom():
        raise ValueError("boom")

    def _private():
        pass
''')


def results(directory: Path) -> list[dict]:
    """The measurands in `directory`, without the runner's load-average siblings."""
    docs = [json.loads(p.read_text()) for p in sorted(directory.glob("*.json")) if not p.name.startswith("workorder-")]
    return [d for d in docs if d["measurement"].get("kind") != "condition"]


def outcome(doc) -> dict:
    return {k: v for k, v in doc["measurement"].items() if k != "quantity"}


@pytest.fixture
def tree(tmp):
    path = tmp / "tree"
    path.mkdir()
    (path / "mod.py").write_text(MODULE)
    return path


def pyperf_order(tree, run_key, case_filter="^f$", **extra):
    document = order(tree, None, run_key, **extra)
    document["target"] = {"kind": "working_tree", "path": str(tree), "source_dir": str(tree),
                          "source": {"uri": SOURCE_URI, "type": "git"}}
    document["suites"] = [{"adapter": "pyperf", "suite": "mod.py", "filter": case_filter}]
    document["precision"] = copy.deepcopy(PRECISION)
    return document


def run_order(tmp, document, name):
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(document))
    runner.run(path, tmp / name)
    return results(tmp / name)


# --- the driving half, end to end -------------------------------------------------

def test_end_to_end_result(tmp, tree):
    docs = run_order(tmp, pyperf_order(tree, "e2e"), "e2e")
    assert [d["coordinates"]["workload"]["name"] for d in docs] == ["f"]
    doc = docs[0]
    assert core.check(doc, "measurement-result") == []
    measurement = doc["measurement"]
    assert measurement["status"] == "success"
    observations = measurement["observations"]
    assert [o["group"] for o in observations] == ["0", "0", "1", "1"]  # one group per worker process
    assert [o["ordinal"] for o in observations] == [0, 1, 2, 3]
    assert doc["procedure"]["inner_iterations"] == 50
    assert doc["procedure"]["attempted_repetitions"] == doc["procedure"]["completed_repetitions"] == 4
    assert doc["procedure"]["warmups_performed"] == 2  # one per worker, dropped
    assert doc["procedure"]["repetition_levels"] == [{"unit": "process", "attempted": 2, "completed": 2},
                                                     {"unit": "value", "attempted": 4, "completed": 4}]
    protocol = doc["coordinates"]["protocol"]
    assert protocol["repetitions"]["levels"] == PRECISION["repetitions"]["levels"]
    assert protocol["environment"] == "inherit-all"
    assert protocol["harness"]["name"] == "pyperf"
    assert doc["coordinates"]["environment"]["system"]["runtime"]["python"]
    kinds = {a["kind"] for a in doc["provenance"]["artifacts"]}
    assert {"native-output", "stdout", "stderr", "driver", "worker-environment"} <= kinds
    assert warnings_of(doc) == []  # the workers had the environment the manager passed
    assert "summaries" not in measurement  # pyperf's file stores none


def test_default_cases_are_bench_prefixed_functions(tmp, tree):
    (tree / "mod.py").write_text("def bench_a():\n    pass\n\ndef benchmark_b():\n    pass\n\ndef helper():\n    pass\n")
    docs = run_order(tmp, pyperf_order(tree, "default-cases", case_filter=None) | {"suites": [
        {"adapter": "pyperf", "suite": "mod.py"}]}, "default-cases")
    assert sorted(d["coordinates"]["workload"]["name"] for d in docs) == ["bench_a", "benchmark_b"]


def test_environment_passes_through_to_the_workers(tmp, tree):
    """The function fails if OPENBLAS_NUM_THREADS is missing; with it set the run succeeds."""
    document = pyperf_order(tree, "env-pass", case_filter="^needs_openblas$")
    missing = run_order(tmp, document, "env-missing")
    assert outcome(missing[0]) == {"status": "error", "reason": "harness.error"}
    assert "OPENBLAS_NUM_THREADS is missing" in missing[0]["provenance"]["info"]["message"]

    document["environment_variables"] = {"OPENBLAS_NUM_THREADS": "1"}
    document["run_key"] = "env-pass-set"
    docs = run_order(tmp, document, "env-set")
    doc = docs[0]
    assert doc["measurement"]["status"] == "success"
    assert doc["coordinates"]["environment"]["system"]["process"]["environment"]["OPENBLAS_NUM_THREADS"] == "1"
    artifact = next(a for a in doc["provenance"]["artifacts"] if a["kind"] == "worker-environment")
    captured = json.loads(Path(artifact["uri"].removeprefix("file://")).read_text())
    assert captured["OPENBLAS_NUM_THREADS"] == "1"
    assert warnings_of(doc) == []


NAMES = snapshot.env_allowlist({}, adapter.ENV_ALLOWLIST)


def test_the_worker_environment_artifact_holds_only_allowlisted_names(tmp, tree):
    env = {"OPENBLAS_NUM_THREADS": "1", "PYTHONHASHSEED": "0", "MY_CI_TOKEN": "hunter2"}
    _, _, run = _case_run(tmp, tree, None, "f", env=env)
    kind, _, path = next(a for a in run["artifacts"] if a[0] == "worker-environment")
    assert "hunter2" not in path.read_text()
    assert run["worker_env"]["OPENBLAS_NUM_THREADS"] == "1" and run["worker_env"]["PYTHONHASHSEED"] == "0"
    assert "MY_CI_TOKEN" not in run["worker_env"]
    assert adapter.observed_environment(run, "f", NAMES)[1] == []  # the digest still covers everything


def test_a_project_name_reaches_the_worker_capture(tmp, tree):
    env = {"BENCHX_ENV_ALLOWLIST": "MY_FLAG", "MY_FLAG": "on"}
    _, _, run = _case_run(tmp, tree, None, "f", env=env)
    assert run["worker_env"]["MY_FLAG"] == "on"


def _case_run(tmp, tree, flags, case, env=None):
    env = {**os.environ, **(env or {})}
    runnable = adapter.locate(tree, "mod.py", env)
    applied, invocation = adapter.protocol(PRECISION)
    native = tmp / "case" / "0000.native.json"
    native.parent.mkdir(exist_ok=True)
    return runnable, env, adapter.run_case(runnable, case, flags if flags is not None else invocation,
                                           env, 60, native)


def test_scrubbing_is_real_without_copy_env(tmp, tree):
    """The negative case: pyperf alone scrubs the environment, so the test above would catch it."""
    _, invocation = adapter.protocol(PRECISION)
    without = [f for f in invocation if f != "--copy-env"]
    _, _, run = _case_run(tmp, tree, without, "needs_openblas", env={"OPENBLAS_NUM_THREADS": "1"})
    assert run["exit_status"] != 0 and run["native"] is None
    assert "OPENBLAS_NUM_THREADS is missing" in run["stderr"]


def test_scrubbed_workers_are_reported_as_env_mismatch(tmp, tree):
    _, invocation = adapter.protocol(PRECISION)
    without = [f for f in invocation if f != "--copy-env"]
    _, _, run = _case_run(tmp, tree, without, "f")
    assert run["native"] is not None
    facts, warnings = adapter.observed_environment(run, "f", NAMES)
    assert warnings == ["env-mismatch"]
    assert facts["process"]["environment"] is not None  # what the workers had, however little


def test_digest_mismatch_with_a_fake_native_file(tmp, tree):
    _, _, run = _case_run(tmp, tree, None, "f")
    assert adapter.observed_environment(run, "f", NAMES)[1] == []
    tampered = copy.deepcopy(run)
    tampered["native"]["metadata"][adapter.ENV_DIGEST_KEY] = "0" * 64
    assert adapter.observed_environment(tampered, "f", NAMES)[1] == ["env-mismatch"]
    missing = copy.deepcopy(run)
    del missing["native"]["metadata"][adapter.ENV_DIGEST_KEY]
    assert adapter.observed_environment(missing, "f", NAMES)[1] == ["env-mismatch"]
    uncaptured = dict(run, worker_env=None)
    facts, warnings = adapter.observed_environment(uncaptured, "f", NAMES)
    # nothing substituted: null, which matches nothing (schema §4.3)
    assert warnings == ["env-capture-missing"] and facts["process"]["environment"] is None


def test_driver_digest_matches_the_adapters(tmp, tree):
    _, env, run = _case_run(tmp, tree, None, "f")
    digests = {md.get(adapter.ENV_DIGEST_KEY) for md in [run["native"]["metadata"],
                                                         *(r["metadata"] for r in run["native"]["benchmarks"][0]["runs"])]
               if adapter.ENV_DIGEST_KEY in md}
    assert digests == {adapter.env_digest(env)} == {run["passed_env_digest"]}  # over the whole environment
    assert adapter.env_digest(run["worker_env"]) != adapter.env_digest(env)  # the capture is filtered


def test_environment_variables_make_distinct_workload_variants(tmp, tree):
    keys = set()
    for threads in ("1", "10"):
        document = pyperf_order(tree, f"variants-{threads}", case_filter="^needs_openblas$")
        document["environment_variables"] = {"OPENBLAS_NUM_THREADS": threads}
        doc = run_order(tmp, document, f"variants-{threads}")[0]
        keys.add(json.dumps(doc["coordinates"]["workload"], sort_keys=True))
    assert len(keys) == 2


def test_import_time_is_not_measured(tmp, tree):
    (tree / "mod.py").write_text("import time\ntime.sleep(1.0)\n\ndef slow_import():\n    time.sleep(0.01)\n")
    doc = run_order(tmp, pyperf_order(tree, "startup", case_filter="slow_import"), "startup")[0]
    values = [o["value"] for o in doc["measurement"]["observations"]]
    assert all(0.009 < v < 0.1 for v in values), values  # loops=50 is per-loop, already divided


def test_a_failing_function_is_an_error_result_with_stderr(tmp, tree):
    doc = run_order(tmp, pyperf_order(tree, "boom", case_filter="^boom$"), "boom")[0]
    assert outcome(doc) == {"status": "error", "reason": "harness.error"}
    assert "ValueError: boom" in doc["provenance"]["info"]["message"]
    stderr = next(a for a in doc["provenance"]["artifacts"] if a["kind"] == "stderr")
    assert "ValueError: boom" in Path(stderr["uri"].removeprefix("file://")).read_text()
    assert not any(a["kind"] == "native-output" for a in doc["provenance"]["artifacts"])


def test_timeout_kills_the_workers_too(tmp, tree):
    (tree / "mod.py").write_text("import time\n\ndef slow():\n    time.sleep(30)\n")
    document = pyperf_order(tree, "timeout", case_filter="slow", timeout_seconds=3)
    doc = run_order(tmp, document, "timeout")[0]
    assert outcome(doc) == {"status": "error", "reason": "timeout"}
    alive = [p for p in Path("/proc").glob("[0-9]*/cmdline") if str(tree).encode() in _read(p)]
    assert alive == []  # no orphaned workers


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:  # the process exited while we looked
        return b""


def _fake_python(directory: Path, body: str) -> dict:
    directory.mkdir(exist_ok=True)
    script = directory / "python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


def test_python_comes_from_the_childs_path_not_sys_executable(tmp, tree, monkeypatch):
    log = tmp / "python.log"
    path = _fake_python(tmp / "fakebin", f'echo "$@" >> {log}\nexec {sys.executable} "$@"')
    monkeypatch.setenv("PATH", path["PATH"])
    doc = run_order(tmp, pyperf_order(tree, "which-python"), "which-python")[0]
    assert doc["measurement"]["status"] == "success"
    assert log.exists() and "driver.py" in log.read_text()


def test_pyperf_must_be_importable_or_the_order_is_refused(tmp, tree, monkeypatch):
    path = _fake_python(tmp / "nopyperf", 'case "$*" in *"import pyperf"*) echo "No module named pyperf" >&2; exit 1;; esac\n'
                                          f'exec {sys.executable} "$@"')
    monkeypatch.setenv("PATH", path["PATH"])
    file = tmp / "o.json"
    file.write_text(json.dumps(pyperf_order(tree, "no-pyperf")))
    with pytest.raises(runner.Refused, match="pyperf is not importable"):
        runner.run(file, tmp / "no-pyperf")
    assert not (tmp / "no-pyperf").exists() or not list((tmp / "no-pyperf").glob("*.json"))


@pytest.mark.parametrize("change, message", [
    (lambda o: o.update(quantities=["cpu-time"]), "quantities"),
    (lambda o: o["precision"].update(repetitions=5), "two levels"),
    (lambda o: o["precision"].update(warmup={"mode": "time", "seconds": 1}), "count-based"),
    (lambda o: o["precision"].update(repetitions={"mode": "fixed", "levels": [{"unit": "repetition", "n": 3}]}),
     "'process' and 'value'"),
    (lambda o: o["suites"][0].update(suite="missing.py"), "not found"),
])
def test_refusals(tmp, tree, change, message):
    document = pyperf_order(tree, "refused")
    change(document)
    file = tmp / "o.json"
    file.write_text(json.dumps(document))
    with pytest.raises(runner.Refused, match=message):
        runner.run(file, tmp / "refused")


def test_an_unimportable_module_is_refused_with_the_reason(tmp, tree):
    (tree / "mod.py").write_text("import not_a_module_anywhere\n")
    file = tmp / "o.json"
    file.write_text(json.dumps(pyperf_order(tree, "bad-import")))
    with pytest.raises(runner.Refused, match="not_a_module_anywhere"):
        runner.run(file, tmp / "bad-import")


# --- the translating half, on captured native output -------------------------------

def _translate(native, attempted=4, exit_status=0, **run):
    record = {"exit_status": exit_status, "timed_out": False, "stdout": "", "stderr": "", "native": native, **run}
    return adapter.translate("f", ["wall-time"], record, attempted)[0]


def test_translating_captured_output():
    native = json.loads((FIXTURES / "pyperf-native.json").read_text())
    out = _translate(native)
    assert out["measurement"]["status"] == "success"
    assert [o["group"] for o in out["measurement"]["observations"]] == ["0", "0", "1", "1"]
    assert out["procedure"] == {"attempted_repetitions": 4, "completed_repetitions": 4, "inner_iterations": 50,
                                "warmups_performed": 2}
    system, info = adapter.context_facts(native)
    assert info["library_version"] == "2.10.0" and "config" in system["cpu"] and system["runtime"]["python"]
    assert adapter.harness(info) == {"name": "pyperf", "version": "2.10.0"}


def test_translation_is_idempotent_and_does_not_mutate_input():
    native = json.loads((FIXTURES / "pyperf-native.json").read_text())
    before = copy.deepcopy(native)
    assert _translate(native) == _translate(native)
    assert native == before


def test_calibration_runs_are_not_observations():
    native = json.loads((FIXTURES / "pyperf-calibrated.json").read_text())
    out = _translate(native, attempted=4)
    assert len(out["measurement"]["observations"]) == 4
    assert out["procedure"]["calibration_runs"] == 1
    assert out["procedure"]["inner_iterations"] == native["metadata"]["loops"]  # what calibration chose
    assert out["procedure"]["warmups_performed"] == 2  # the calibration run's warmups are not counted


def test_fewer_runs_than_requested_is_partial():
    native = json.loads((FIXTURES / "pyperf-native.json").read_text())
    del native["benchmarks"][0]["runs"][-1]
    out = _translate(native, attempted=4)
    assert out["measurement"]["status"] == "partial" and out["measurement"]["reason"] == "harness.partial"
    assert out["procedure"]["completed_repetitions"] == 2


def _levels(native, **run):
    record = {"exit_status": 0, "timed_out": False, "native": native, **run}
    return adapter.repetition_levels(PRECISION, record, "f")


def test_a_lost_process_shows_at_the_process_level():
    native = json.loads((FIXTURES / "pyperf-native.json").read_text())
    assert [lv["completed"] for lv in _levels(native)] == [2, 4]
    del native["benchmarks"][0]["runs"][-1]
    assert _levels(native) == [{"unit": "process", "attempted": 2, "completed": 1},
                               {"unit": "value", "attempted": 4, "completed": 2}]


def test_no_output_completes_no_level():
    assert _levels(None) == [{"unit": "process", "attempted": 2, "completed": 0},
                             {"unit": "value", "attempted": 4, "completed": 0}]


def test_a_missing_output_file_is_an_error_result():
    out = _translate(None, exit_status=1, stderr="Traceback\nValueError: boom\n")
    assert out["measurement"] == {"status": "error", "reason": "harness.error"}
    assert out["info"]["message"] == "ValueError: boom"
    assert _translate(None, exit_status=0)["measurement"] == {"status": "error", "reason": "harness.no-output"}


def test_a_non_time_unit_is_a_mapping_error():
    native = json.loads((FIXTURES / "pyperf-native.json").read_text())
    native["metadata"]["unit"] = "byte"
    assert _translate(native)["measurement"] == {"status": "error", "reason": "adapter.mapping-failed"}


def test_pythonhashseed_is_recorded_when_set(tmp, tree):
    document = pyperf_order(tree, "hashseed")
    document["environment_variables"] = {"PYTHONHASHSEED": "7"}
    doc = run_order(tmp, document, "hashseed")[0]
    assert doc["measurement"]["status"] == "success"
    assert doc["coordinates"]["environment"]["system"]["process"]["environment"]["PYTHONHASHSEED"] == "7"


def test_relative_output_directory(tmp, tree, monkeypatch):
    """The workers run in the tree, so the driver and output paths must not depend on the caller's cwd."""
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    order_path = tmp / "rel.json"
    order_path.write_text(json.dumps(pyperf_order(tree, "rel")))
    runner.run(order_path, Path("rel-out"))
    docs = results(elsewhere / "rel-out")
    assert [d["measurement"]["status"] for d in docs] == ["success"]
