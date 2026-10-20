"""pyperf adapter (harness-adapter.md §6.8), both halves.

Driving: the suite is a Python module inside the target; a case is one of its
functions. Each case gets a generated driver script (kept as an artifact) that
runs `Runner.bench_func` in a child interpreter resolved from the child's own
PATH. pyperf must be importable there, or the order is refused. Translating:
one case's pyperf JSON (`-o`) to one measurement and procedure.

The driver records the environment it actually had, before it imports the
user's module: a digest in every worker's run metadata and, from a worker,
the full environment as an artifact. The adapter reports what the workers had, not what the manager
passed, and warns `env-mismatch` when the two differ.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .. import snapshot
from .base import Unsupported

NAME = "pyperf"
# The suite is a Python source file, which may live outside the subject's checkout.
SUITE_IS_SOURCE = True
CONTEXT_KEY = "pyperf"
WORKLOAD_PARAMETERS = False
PRODUCER = {"name": "benchx/pyperf-adapter", "version": "0.1.0", "mapping_version": "pyperf-to-benchx/v1"}

QUANTITIES = {
    "wall-time": ("values", {"name": "wall-time", "unit": "s", "direction": "lower-is-better"}),
}

# pyperf's own defaults, recorded when the order leaves them out, so no applied
# setting goes unrecorded.
DEFAULTS = {
    "processes": 20,
    "values": 3,
    "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.1},
    "warmup": {"mode": "count", "n_warmup": 1},
}
# With no suites[].filter, the module's functions named like this are the cases.
DEFAULT_CASE_PREFIXES = ("bench_", "benchmark_")

ENV_DIGEST_KEY = "benchx_env_sha256"
# Variables recorded beside the core and project allowlist when set in a worker.
ENV_ALLOWLIST = ("PYTHONHASHSEED",)


def env_digest(env: dict) -> str:
    """sha256 over the canonical serialization of an environment. The driver
    computes the same thing with the same expression (see DRIVER)."""
    text = json.dumps(dict(env), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def protocol(precision: dict) -> tuple[dict, list[str]]:
    """The full protocol that will apply, and the pyperf flags that apply it.

    pyperf repeats at two levels: worker processes, each taking a number of
    values. A level the order leaves out takes pyperf's default. `--copy-env`
    is always passed: every worker inherits the full environment (the user
    prepared it; nothing is scrubbed), and that choice is part of the protocol.
    """
    problems = []
    repetitions = precision["repetitions"]
    given = {}
    if isinstance(repetitions, int):
        problems.append("pyperf repeats at two levels; give repetitions as "
                        "{mode: fixed, levels: [{unit: process, n}, {unit: value, n}]}")
    else:
        for level in repetitions["levels"]:
            if level["unit"] not in ("process", "value") or level["unit"] in given:
                problems.append("pyperf repetition levels are 'process' and 'value', once each")
                break
            given[level["unit"]] = level["n"]
    processes, values = given.get("process", DEFAULTS["processes"]), given.get("value", DEFAULTS["values"])

    calibration = precision.get("calibration", DEFAULTS["calibration"])
    warmup = precision.get("warmup", DEFAULTS["warmup"])
    flags = ["--processes", str(processes), "--values", str(values)]
    if calibration["mode"] == "fixed":
        flags += ["--loops", str(calibration["n_iterations"])]
    else:
        flags += ["--min-time", str(calibration["minimum_sample_seconds"])]
    if warmup["mode"] == "time":
        problems.append("pyperf warmup is count-based only, not time-based")
    if problems:
        raise Unsupported(*problems)
    flags += ["--warmups", str(warmup["n_warmup"] if warmup["mode"] == "count" else 0)]
    flags.append("--copy-env")

    applied = {
        "repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": processes},
                                                   {"unit": "value", "n": values}]},
        "calibration": calibration,
        "warmup": warmup,
        "timer": "perf_counter",
        "gc": "enabled",  # pyperf's loop leaves the collector as found; python_gc is recorded if a module disables it
        "environment": "inherit-all",
    }
    return applied, flags


def _which_python(env: dict) -> str | None:
    path = env.get("PATH", "")
    return shutil.which("python", path=path) or shutil.which("python3", path=path)


def locate(target_path, suite, env) -> dict:
    """The module to benchmark and the interpreter that will run it.

    `python` comes from the child's PATH, never from `sys.executable`. pyperf
    must be importable there; if it is not, nothing runs.
    """
    root = Path(target_path)
    module = root / suite
    if not module.is_file():
        raise Unsupported(f"suite module not found: {module}")
    python = _which_python(env)
    if python is None:
        raise Unsupported("no `python` on the PATH the benchmark would run with")
    probe = subprocess.run([python, "-c", "import pyperf"], env=env, capture_output=True, text=True)
    if probe.returncode != 0:
        raise Unsupported(f"pyperf is not importable by {python}: {probe.stderr.strip().splitlines()[-1:]}")
    return {"python": python, "module": module.resolve(), "root": root.resolve()}


LISTER = r"""
import importlib.util, json, re, sys
module_path, case_filter, prefixes = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
import os
sys.path[:0] = [os.path.dirname(module_path), os.getcwd()]
spec = importlib.util.spec_from_file_location("benchx_suite", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules["benchx_suite"] = module
spec.loader.exec_module(module)
names = [n for n, f in vars(module).items()
         if callable(f) and getattr(f, "__module__", None) == "benchx_suite" and not n.startswith("_")
         and (re.search(case_filter, n) if case_filter else n.startswith(tuple(prefixes)))]
print(json.dumps(sorted(names)))
"""


def list_cases(runnable, case_filter, env) -> list[str]:
    """The module's functions: those matching the filter (a regex searched in the
    name), or with no filter those named `bench_*` or `benchmark_*`."""
    out = subprocess.run([runnable["python"], "-c", LISTER, str(runnable["module"]), case_filter or "",
                          json.dumps(DEFAULT_CASE_PREFIXES)],
                         env=env, cwd=runnable["root"], capture_output=True, text=True)
    if out.returncode != 0:
        raise Unsupported(f"cannot import {runnable['module'].name}: "
                          f"{(out.stderr.strip().splitlines() or ['no output'])[-1]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


DRIVER = '''\
# Generated by benchx. Runs one function under pyperf.
import hashlib, json, os, sys
from pathlib import Path

# Before anything of the user's is imported: what this process was given.
_ENV = dict(os.environ)
_DIGEST = hashlib.sha256(
    json.dumps(_ENV, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

import importlib.util
import pyperf

runner = pyperf.Runner()
runner.parse_args()
runner.metadata[{digest_key!r}] = _DIGEST
if runner.args.worker:
    # Only the allowlisted names: the digest above covers the whole environment,
    # the file is shared, and CI environments carry secrets.
    Path({worker_env!r}).write_text(json.dumps({{k: _ENV[k] for k in {env_names!r} if k in _ENV}}, sort_keys=True, indent=1))

sys.path[:0] = [{module_dir!r}, {root!r}]
spec = importlib.util.spec_from_file_location("benchx_suite", {module!r})
module = importlib.util.module_from_spec(spec)
sys.modules["benchx_suite"] = module
spec.loader.exec_module(module)
runner.bench_func({case!r}, getattr(module, {case!r}))
'''


def run_case(runnable, case, flags, env, timeout, native_path, env_names=None) -> dict:
    """Run one case; never raises for harness failures, which become results."""
    if env_names is None:
        env_names = snapshot.env_allowlist(env, ENV_ALLOWLIST)
    stem = native_path.name.removesuffix(".native.json")
    driver = native_path.with_name(f"{stem}.driver.py")
    worker_env = native_path.with_name(f"{stem}.worker-env.json")
    for stale in (native_path, worker_env):
        stale.unlink(missing_ok=True)
    driver.write_text(DRIVER.format(
        digest_key=ENV_DIGEST_KEY, worker_env=str(worker_env.resolve()), env_names=list(env_names), module=str(runnable["module"]),
        module_dir=str(runnable["module"].parent), root=str(runnable["root"]), case=case))
    # Absolute: the child runs in the tree, not where the output directory was named.
    args = [runnable["python"], str(driver.resolve()), *flags, "-o", str(native_path.resolve())]
    # Its own process group, so a timeout takes the workers down with the manager.
    proc = subprocess.Popen(args, env=env, cwd=runnable["root"], text=True, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        status = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        status = None
    native = None
    if native_path.exists():
        try:
            native = json.loads(native_path.read_text())
            native_path.write_text(json.dumps(native, indent=1) + "\n")
        except ValueError:
            native = None
    captured = None
    if worker_env.exists():
        try:
            captured = json.loads(worker_env.read_text())
        except ValueError:
            captured = None
    artifacts = [("driver", "text/x-python", driver)]
    if worker_env.exists():
        artifacts.append(("worker-environment", "application/json", worker_env))
    return {"exit_status": status, "timed_out": status is None, "stdout": stdout or "", "stderr": stderr or "",
            "native": native, "artifacts": artifacts,
            "worker_env": captured, "passed_env_digest": env_digest(env)}


def _failure_message(stderr: str) -> str:
    """The worker's exception line. The manager then raises its own
    "<python> failed with exit code N" after a second traceback; that wrapper
    only counts when nothing else was said."""
    lines = [l for l in stderr.splitlines() if l and not l[0].isspace() and not l.startswith("Traceback")]
    own = [l for l in lines if "failed with exit code" not in l]
    return (own or lines or [stderr.strip().splitlines()[-1]])[0]


def attempted(applied: dict) -> int:
    """Values requested: processes times values per process."""
    n = 1
    for level in applied["repetitions"]["levels"]:
        n *= level["n"]
    return n


def repetition_levels(applied: dict, run: dict, case: str) -> list[dict]:
    """Planned and realized counts per repetition level, outermost first. Counts
    are totals over the attempt: a level's attempted is the product of the
    requested levels down to it, so 20 processes of 3 values attempts 20 and 60.
    A process is completed when it produced values; calibration runs are neither."""
    benchmark = _benchmark(run["native"], case)
    measured = [r for r, _ in _runs(run["native"], benchmark) if r.get("values")] if benchmark else []
    realized = {"process": len(measured), "value": sum(len(r["values"]) for r in measured)}
    levels, planned = [], 1
    for level in applied["repetitions"]["levels"]:
        planned *= level["n"]
        levels.append({"unit": level["unit"], "attempted": planned, "completed": realized[level["unit"]]})
    return levels


def harness(info: dict) -> dict:
    out = {"name": NAME}
    if info.get("library_version"):
        out["version"] = info["library_version"]
    return out


def comparison_extras(info: dict) -> dict:
    """Host runtime facts that belong to comparison context (§6.8)."""
    runtime = {k: info[k] for k in ("python_version", "python_implementation", "python_compiler",
                                    "python_cflags") if k in info}
    return {"runtime": runtime} if runtime else {}


def _effective(native: dict, benchmark: dict, run: dict) -> dict:
    """File, benchmark and run metadata; the most specific level wins."""
    return {**native.get("metadata", {}), **benchmark.get("metadata", {}), **run.get("metadata", {})}


def _benchmark(native: dict | None, case: str) -> dict | None:
    for benchmark in (native or {}).get("benchmarks", []):
        name = {**native.get("metadata", {}), **benchmark.get("metadata", {})}.get("name")
        if name == case:
            return benchmark
    return None


def _runs(native: dict, benchmark: dict) -> list[tuple[dict, dict]]:
    return [(run, _effective(native, benchmark, run)) for run in benchmark.get("runs", [])]


def translate(case: str, quantities: list[str], run: dict, attempted: int) -> list[dict]:
    """One case's native output to a measurement and procedure per quantity."""
    native = run["native"]
    benchmark = _benchmark(native, case)
    runs = _runs(native, benchmark) if benchmark else []
    measured = [(r, md) for r, md in runs if r.get("values")]
    calibration = [(r, md) for r, md in runs if not r.get("values") and ("calibrate_loops" in r["metadata"]
                                                                         or "recalibrate_loops" in r["metadata"])]
    outputs = []
    for name in quantities:
        _, quantity = QUANTITIES[name]
        observations, loops = [], set()
        for index, (r, md) in enumerate(measured):
            loops.add(md.get("loops", 1) * md.get("inner_loops", 1))
            for value in r["values"]:
                observations.append({"value": value, "ordinal": len(observations), "group": str(index)})
        procedure = {"attempted_repetitions": attempted, "completed_repetitions": len(observations)}
        info = {}
        units = {md.get("unit") for _, md in measured}
        if run["timed_out"]:
            measurement = {"status": "error", "reason": "timeout"}
        elif not observations:
            failed = run["exit_status"] not in (0, None)
            measurement = {"status": "error", "reason": "harness.error" if failed else "harness.no-output"}
            if failed and run["stderr"].strip():
                info["message"] = _failure_message(run["stderr"])
        elif units != {"second"}:
            measurement = {"status": "error", "reason": "adapter.mapping-failed"}
            info["message"] = f"unit {sorted(map(str, units))} is not 'second'; only wall-time is mapped"
        else:
            measurement = {"status": "success", "observations": observations}
            if len(observations) < attempted:
                measurement.update(status="partial", reason="harness.partial")
            if len(loops) == 1:
                procedure["inner_iterations"] = loops.pop()
            else:
                info["inner_iterations_per_run"] = [md.get("loops", 1) * md.get("inner_loops", 1)
                                                    for _, md in measured]
            procedure["warmups_performed"] = sum(len(r.get("warmups", [])) for r, _ in measured)
            if calibration:
                procedure["calibration_runs"] = len(calibration)
        if run["exit_status"] not in (0, None):
            info["exit_status"] = run["exit_status"]
        outputs.append({"quantity": quantity, "measurement": measurement, "procedure": procedure, "info": info})
    return outputs


def context_facts(native: dict | None) -> tuple[dict, dict]:
    """The native metadata split by meaning: (observed context, provenance info).

    The file-level metadata: what held for every run. Facts that differ per
    run (load, frequency, temperature) stay in the native artifact.
    """
    md = (native or {}).get("metadata", {})
    observed = {k: md[k] for k in ("cpu_config", "cpu_affinity", "aslr", "platform", "boot_time",
                                   "python_gc") if k in md}
    info = {k: md[k] for k in ("hostname", "cpu_model_name", "cpu_count", "python_executable") if k in md}
    if "perf_version" in md:
        info["library_version"] = md["perf_version"]
    for k in ("python_version", "python_implementation", "python_compiler", "python_cflags"):
        if k in md:
            info[k] = md[k]
    return observed, info


def observed_environment(run: dict, case: str, env_names) -> tuple[dict, list[str]]:
    """The environment the workers actually had, and the warnings about it.

    Facts come from the worker-side capture, never from what the manager
    passed. A digest in every run is compared with the digest of what the
    manager passed; a mismatch or a missing digest is `env-mismatch`. When
    nothing was captured, the observed environment is absent (None), never
    substituted by the manager's.
    """
    warnings = []
    captured = run.get("worker_env")
    facts = {"env": None}
    if captured is not None:
        facts["env"] = {n: captured[n] for n in env_names if n in captured}
    native = run["native"]
    benchmark = _benchmark(native, case)
    if benchmark is not None:
        digests = {md.get(ENV_DIGEST_KEY) for r, md in _runs(native, benchmark) if r.get("values")}
        if digests != {run["passed_env_digest"]}:
            warnings.append("env-mismatch")
        if captured is None:
            warnings.append("env-capture-missing")
    return facts, warnings


def default_precision() -> dict:
    """pyperf's own defaults, spelled as a work-order `precision`."""
    return {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": DEFAULTS["processes"]},
                                                       {"unit": "value", "n": DEFAULTS["values"]}]},
            "calibration": dict(DEFAULTS["calibration"]), "warmup": dict(DEFAULTS["warmup"])}
