"""Google Benchmark adapter (harness-adapter.md §6.1), both halves.

Driving: list the planned cases, then run each case in its own process so a
timeout, crash, or error is scoped to one workload variant. Translating: one
case's native JSON to one measurement and procedure per requested quantity.
The runner supplies everything else a result needs (the context document).
"""

import json
import os
import re
import subprocess
from pathlib import Path

from .base import Unsupported

NAME = "google-benchmark"
# The suite is a binary built from the subject's source, so its benchmarks are the subject's own.
SUITE_IS_SOURCE = False
CONTEXT_KEY = "google_benchmark"
WORKLOAD_PARAMETERS = False
PRODUCER = {"name": "benchx/gbench-adapter", "version": "0.1.0", "mapping_version": "gbench-to-benchx/v1"}

# Native field, canonical quantity (Appendix A), and direction.
QUANTITIES = {
    "wall-time": ("real_time", {"name": "wall-time", "unit": "s", "direction": "lower-is-better"}),
    "cpu-time": ("cpu_time", {"name": "cpu-time", "unit": "s", "direction": "lower-is-better"}),
}
_TO_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}

# Google Benchmark's own defaults, recorded when the order leaves them out, so
# no applied setting goes unrecorded (work-order §3 "every applied setting is
# recorded"). precision.repetitions has no default: it is required.
DEFAULT_REPETITIONS = 5  # the order always states a repetition count
DEFAULTS = {
    "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.5},
    "warmup": {"mode": "none"},
}


def default_precision() -> dict:
    """The precision a Google Benchmark order gets with nothing configured."""
    return {"repetitions": DEFAULT_REPETITIONS, "calibration": dict(DEFAULTS["calibration"]),
            "warmup": dict(DEFAULTS["warmup"])}


def protocol(precision: dict) -> tuple[dict, list[str]]:
    """The full protocol that will apply, and the flags that apply it.

    precision is already schema-valid (work-order §3): repetitions is a bare
    int or {mode: fixed, levels[]}; calibration and warmup, when present, are
    mode-tagged per the result schema's Appendix B. Unknown keys are already
    rejected by the schema, so only gbench-specific unsupported shapes are
    checked here.
    """
    problems = []
    repetitions = precision["repetitions"]
    levels = [{"unit": "repetition", "n": repetitions}] if isinstance(repetitions, int) else repetitions["levels"]
    if len(levels) != 1 or levels[0]["unit"] != "repetition":
        problems.append("google-benchmark repeats at one level, unit 'repetition'")
    applied = {
        "repetitions": {"mode": "fixed", "levels": levels},
        "calibration": precision.get("calibration", DEFAULTS["calibration"]),
        "warmup": precision.get("warmup", DEFAULTS["warmup"]),
    }
    flags = [f"--benchmark_repetitions={levels[0]['n']}"]

    calibration = applied["calibration"]
    if calibration["mode"] == "adaptive":
        flags.append(f"--benchmark_min_time={calibration['minimum_sample_seconds']}s")
    else:
        flags.append(f"--benchmark_min_time={calibration['n_iterations']}x")

    warmup = applied["warmup"]
    if warmup["mode"] == "time":
        flags.append(f"--benchmark_min_warmup_time={warmup['seconds']}")
    elif warmup["mode"] == "count":
        problems.append("google-benchmark warmup is time-based only, not count-based")
    elif warmup["mode"] != "none":
        problems.append(f"unknown warmup mode: {warmup['mode']}")
    if problems:
        raise Unsupported(*problems)
    return applied, flags


def locate(target_path, suite, env) -> Path:
    """The benchmark binary: the suite's name inside the target's build directory."""
    binary = Path(target_path) / suite
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        raise Unsupported(f"suite binary not found: {binary}")
    return binary


def attempted(applied: dict) -> int:
    """The repetitions asked for; protocol() already allows only one level."""
    return applied["repetitions"]["levels"][0]["n"]


def harness(info: dict) -> dict:
    """protocol.harness, from the provenance info context_facts gave."""
    out = {"name": NAME}
    if info.get("library_version"):
        out["version"] = info["library_version"]
    return out


def list_cases(binary, case_filter, env) -> list[str]:
    """The planned set, fixed before running (harness-adapter.md §5 step 5)."""
    args = [str(binary), "--benchmark_list_tests=true"]
    if case_filter:
        args.append(f"--benchmark_filter={case_filter}")
    out = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def run_case(binary, case, flags, env, timeout, native_path, env_names=()) -> dict:
    """Run one case; never raises for harness failures, which become results."""
    args = [str(binary), f"--benchmark_filter=^{re.escape(case)}$",
            f"--benchmark_out={native_path}", "--benchmark_out_format=json", *flags]
    try:
        out = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
        status, stdout, stderr = out.returncode, out.stdout, out.stderr
    except subprocess.TimeoutExpired as e:
        status, stdout, stderr = None, e.stdout or "", e.stderr or ""
        stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode() if isinstance(stderr, bytes) else stderr
    native = None
    if native_path.exists():
        try:
            native = json.loads(native_path.read_text())
        except ValueError:
            native = None
    return {"exit_status": status, "timed_out": status is None, "stdout": stdout, "stderr": stderr,
            "native": native}


def translate(case: str, quantities: list[str], run: dict, attempted: int) -> list[dict]:
    """One case's native output to a measurement and procedure per quantity."""
    native = run["native"] or {}
    rows = [r for r in native.get("benchmarks", []) if r.get("run_name", r.get("name")) == case]
    iterations = [r for r in rows if r.get("run_type", "iteration") == "iteration"]
    failed = [r for r in iterations if r.get("error_occurred")]
    skipped = [r for r in iterations if r.get("skipped")]
    good = [r for r in iterations if not r.get("error_occurred") and not r.get("skipped")]
    aggregates = {r.get("aggregate_name"): r for r in rows if r.get("run_type") == "aggregate"}

    outputs = []
    for name in quantities:
        field, quantity = QUANTITIES[name]
        procedure = {"attempted_repetitions": attempted, "completed_repetitions": len(good)}
        info = {}
        if run["timed_out"]:
            measurement = {"status": "error", "reason": "timeout"}
        elif skipped and not good:
            measurement = {"status": "skipped", "reason": "harness.skipped"}
            info["message"] = skipped[0].get("skip_message") or skipped[0].get("error_message")
        elif failed and not good:
            measurement = {"status": "error", "reason": "harness.error"}
            info["message"] = failed[0].get("error_message")
        elif not good:
            measurement = {"status": "error", "reason": "harness.no-output"}
        else:
            observations = [r[field] * _TO_SECONDS[r.get("time_unit", "ns")] for r in good]
            measurement = {"status": "success", "observations": observations}
            if len(good) < attempted:
                measurement.update(status="partial", reason="harness.partial")
            summaries = _summaries(aggregates, field)
            if summaries:
                measurement["summaries"] = summaries
            counts = {r.get("iterations") for r in good}
            if len(counts) == 1 and None not in counts:
                procedure["inner_iterations"] = counts.pop()
            else:
                info["iterations"] = [r.get("iterations") for r in good]
        if run["exit_status"] not in (0, None):
            info["exit_status"] = run["exit_status"]
        outputs.append({"quantity": quantity, "measurement": measurement,
                        "procedure": procedure, "info": info})
    return outputs


def _summaries(aggregates: dict, field: str) -> list[dict]:
    """Time aggregates as typed producer summaries; percentage ones are skipped."""
    summaries = []
    for name, summary_type in (("mean", "estimate"), ("median", "estimate"), ("stddev", "statistic")):
        row = aggregates.get(name)
        if not row or row.get("aggregate_unit", "time") != "time":
            continue
        value = row[field] * _TO_SECONDS[row.get("time_unit", "ns")]
        if summary_type == "estimate":
            summaries.append({"type": "estimate", "source": "producer", "value": value, "estimator": {
                "name": name, "method_version": f"google-benchmark/{name}", "input_level": "observations"}})
        else:
            summaries.append({"type": "statistic", "source": "producer", "name": name, "value": value})
    return summaries


def context_facts(native: dict | None) -> tuple[dict, dict]:
    """The native context split by meaning: (environment.system facts, provenance
    info). The load average is a condition the runner measures itself (schema
    §4.4); the harness's copy stays in the native artifact."""
    context = (native or {}).get("context", {})
    system = {"cpu": {"scaling_enabled": context["cpu_scaling_enabled"]}} if "cpu_scaling_enabled" in context else {}
    info = {k: context[k] for k in ("host_name", "num_cpus", "mhz_per_cpu", "executable",
                                     "library_build_type", "library_version", "date") if k in context}
    return system, info
