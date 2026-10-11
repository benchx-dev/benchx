"""Google Benchmark adapter (harness-adapter.md §6.1), both halves.

Driving: list the planned cases, then run each case in its own process so a
timeout, crash, or error is scoped to one workload variant. Translating: one
case's native JSON to one measurement and procedure per requested quantity.
The runner supplies everything else a result needs (the context document).
"""

import json
import re
import subprocess
import time

from ..core import Refusal, Rejected

PRODUCER = {"name": "benchx/gbench-adapter", "version": "0.1.0", "mapping_version": "gbench-to-benchx/v1"}

# Native field, canonical quantity (Appendix A), and direction.
QUANTITIES = {
    "wall-time": ("real_time", {"name": "wall-time", "unit": "s", "direction": "lower-is-better"}),
    "cpu-time": ("cpu_time", {"name": "cpu-time", "unit": "s", "direction": "lower-is-better"}),
}
_TO_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}

# Google Benchmark's own defaults, recorded when the order leaves them out, so
# no applied setting goes unrecorded (#35 W4a).
DEFAULT_PROTOCOL = {
    "repetitions": {"mode": "fixed", "levels": [{"unit": "repetition", "n": 1}]},
    "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.5},
    "warmup": {"mode": "none"},
}


class Unsupported(Rejected):
    """An order this adapter cannot apply exactly; the runner refuses it."""


def protocol(order_protocol: dict | None) -> tuple[dict, list[str]]:
    """The full protocol that will apply, and the flags that apply it."""
    requested = dict(order_protocol or {})
    problems = []
    unknown = set(requested) - set(DEFAULT_PROTOCOL)
    if unknown:
        problems.append(Refusal(f"protocol keys not supported by google-benchmark: {sorted(unknown)}"))
    applied = {**DEFAULT_PROTOCOL, **requested}
    flags = []

    levels = applied["repetitions"]["levels"]
    if len(levels) != 1 or levels[0]["unit"] != "repetition":
        problems.append(Refusal("google-benchmark repeats at one level, unit 'repetition'"))
    else:
        flags.append(f"--benchmark_repetitions={levels[0]['n']}")

    calibration = applied["calibration"]
    if calibration["mode"] == "adaptive":
        flags.append(f"--benchmark_min_time={calibration['minimum_sample_seconds']}s")
    else:
        flags.append(f"--benchmark_min_time={calibration['n_iterations']}x")

    warmup = applied["warmup"]
    if warmup["mode"] == "time":
        flags.append(f"--benchmark_min_warmup_time={warmup['seconds']}")
    elif warmup["mode"] != "none":
        problems.append(Refusal("google-benchmark warms up by time only"))
    if problems:
        raise Unsupported(problems)
    return applied, flags


def list_cases(binary, case_filter, env) -> list[str]:
    """The planned set, fixed before running (harness-adapter.md §5 step 5)."""
    args = [str(binary), "--benchmark_list_tests=true"]
    if case_filter:
        args.append(f"--benchmark_filter={case_filter}")
    out = subprocess.run(args, env=env, capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def run_case(binary, case, flags, env, timeout, native_path) -> dict:
    """Run one case; never raises for harness failures, which become results."""
    args = [str(binary), f"--benchmark_filter=^{re.escape(case)}$",
            f"--benchmark_out={native_path}", "--benchmark_out_format=json", *flags]
    started = time.time()
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
            "native": native, "started": started, "ended": time.time()}


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
    """The native context split by meaning: (observed context, provenance info)."""
    context = (native or {}).get("context", {})
    observed = {k: context[k] for k in ("cpu_scaling_enabled", "load_avg") if k in context}
    info = {k: context[k] for k in ("host_name", "num_cpus", "mhz_per_cpu", "executable",
                                     "library_build_type", "library_version", "date") if k in context}
    return observed, info
