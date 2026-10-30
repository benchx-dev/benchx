"""ASV adapter (harness-adapter.md §6.7): asv's benchmark language, timed by pyperf.

ASV is treated as a way of writing benchmarks, not as a harness to drive.
Nothing of asv's is used, neither asv itself nor asv_runner: `asv_runtime`
(our own, standard library only) reads the suite as asv defines it, and each
case is timed by pyperf through `Runner.bench_time_func`. The harness in every
result is therefore pyperf, and the translating half is the pyperf adapter's.

Driving: the suite is an ASV benchmark directory inside the target. The
benchmarks run in the interpreter first on the child's PATH, which must import
pyperf and the project being measured. A case is one benchmark and, for a
parameterized one, one parameter combination, named as asv names it
(`module.Class.time_x-3`). Each case is one pyperf run from a scratch
directory outside the tree (so an unbuilt source checkout cannot shadow the
installed project): the pyperf manager runs `setup_cache` once when there is
one, and each worker runs the setups, then pyperf's warmups and values. Only
`time_*` benchmarks are cases; `peakmem_*`, `mem_*`, `track_*` and `timeraw_*`
are listed as not run, since only wall time is mapped.

Precision: the work order's levels win; what it leaves out, each benchmark's
own asv attributes decide, as far as pyperf can express them. `rounds` is not
read (the order always gives processes); `repeat` gives values (a `(min, max,
seconds)` repeat gives its minimum: pyperf takes a fixed number); `number`
gives fixed loops; `sample_time` gives pyperf's `--min-time`; `warmup_time = 0`
means no warmup, any other a single warmup value; `timeout` is pyperf's
per-worker `--timeout`, rounded up to whole seconds (asv's default 60 s). Each
result reports its own case's protocol (`case_protocol`).

Setup scope: a benchmark with setup hooks (its own `setup`, or a module-level
one) declares how often they run with `setup_scope`, looked up like the other
attributes (function, class, module). "call", the default, is asv_runner's
since 0.3.0 (asv#966, asv_runner#53): setup runs before every timed call, so
every call sees freshly set-up state; with no `number` that is one call per
loop, and with one, setup runs again between calls outside the timed region.
"sample", as proposed in asv#1613, runs setup once per value and lets the
calls of a value share its state, so pyperf calibrates loops as usual. It is
for benchmarks whose calls leave the set-up state as they found it; for a
fast one under "call", the timing is mostly timer overhead and cold caches,
and the result carries the `setup-per-call-unbatched` warning.

Translating: pyperf's JSON as the pyperf adapter reads it; parameter values
are kept as asv's repr() strings; asv's `version` (the same hash asv computes)
is part of the protocol, so editing a benchmark starts a new series.
"""

import itertools
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import snapshot
from . import pyperf
from .base import Unsupported, no_cases, run_bounded

NAME = "asv"
CONTEXT_KEY = "asv"
WORKLOAD_PARAMETERS = False
PRODUCER = {"name": "benchx/asv-adapter", "version": "0.2.0", "mapping_version": "asv-on-pyperf/v2"}
QUANTITIES = pyperf.QUANTITIES
ENV_ALLOWLIST = pyperf.ENV_ALLOWLIST

RUNTIME = Path(__file__).with_name("asv_runtime.py")
CONF_NAMES = ("asv.conf.json", "asv.conf.jsonc")
# asv's own defaults, where an order and a benchmark both leave a setting out.
DEFAULT_ROUNDS = 2
DEFAULT_VALUES = 5  # asv's adaptive repeat takes at most 5 samples per round when rounds > 1
DEFAULT_SAMPLE_TIME = 0.01
DEFAULT_TIMEOUT = 60.0
SETUP_SCOPES = ("call", "sample")
UNBATCHED = "setup-per-call-unbatched"
# pyperf exits with this when a worker overruns --timeout.
PYPERF_TIMEOUT_EXIT = 124


def default_precision() -> dict:
    """Only the process level: values, calibration and warmup are left to each benchmark."""
    return {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": DEFAULT_ROUNDS}]}}


def protocol(precision: dict) -> tuple[dict, dict]:
    """The order-level protocol, and what of it each case must apply.

    The order is pyperf's vocabulary: `process` and `value` levels, fixed
    (`--loops`) or adaptive (`--min-time`) calibration, count warmup. What it
    leaves out is None in the invocation, decided per benchmark (`_plan`).
    """
    problems, given = [], {}
    repetitions = precision["repetitions"]
    if isinstance(repetitions, int):
        problems.append("pyperf repeats at two levels; give repetitions as "
                        "{mode: fixed, levels: [{unit: process, n}, {unit: value, n}]}")
    else:
        for level in repetitions["levels"]:
            if level["unit"] not in ("process", "value") or level["unit"] in given:
                problems.append("pyperf repetition levels are 'process' and 'value', once each")
                break
            given[level["unit"]] = level["n"]
    invocation = {"processes": given.get("process", DEFAULT_ROUNDS), "values": given.get("value"),
                  "loops": None, "min_time": None, "warmups": None}
    calibration = precision.get("calibration")
    if calibration:
        if calibration["mode"] == "fixed":
            invocation["loops"] = calibration["n_iterations"]
        else:
            invocation["min_time"] = calibration["minimum_sample_seconds"]
    warmup = precision.get("warmup")
    if warmup:
        if warmup["mode"] == "time":
            problems.append("pyperf warmup is count-based only, not time-based")
        else:
            invocation["warmups"] = warmup.get("n_warmup", 0)
    if problems:
        raise Unsupported(*problems)
    levels = [{"unit": "process", "n": invocation["processes"]}]
    if invocation["values"] is not None:
        levels.append({"unit": "value", "n": invocation["values"]})
    applied = {"repetitions": {"mode": "fixed", "levels": levels}, "timer": "perf_counter", "gc": "disabled",
               "environment": "inherit-all"}
    if calibration:
        applied["calibration"] = calibration
    if warmup:
        applied["warmup"] = warmup
    return applied, invocation


def _which_python(env: dict) -> str | None:
    path = env.get("PATH", "")
    return shutil.which("python", path=path) or shutil.which("python3", path=path)


def _read_conf(path: Path) -> dict:
    """asv.conf.json allows // comments; drop whole-line ones, as asv does."""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("//")]
    try:
        return json.loads("\n".join(lines))
    except ValueError:
        return {}


def _project(benchmark_dir: Path, root: Path) -> str | None:
    """The `project` of the nearest asv.conf.json at or above the benchmark directory, within the tree."""
    for directory in [benchmark_dir, *benchmark_dir.parents]:
        for name in CONF_NAMES:
            if (directory / name).is_file():
                return _read_conf(directory / name).get("project")
        if directory == root:
            break
    return None


def locate(target_path, suite, env, timeout=None) -> dict:
    """The benchmark directory, the interpreter that will run it, and the project it measures."""
    root = Path(target_path).resolve()
    benchmark_dir = (root / suite).resolve()
    if not benchmark_dir.is_dir():
        raise Unsupported(f"suite is not an asv benchmark directory: {benchmark_dir}")
    python = _which_python(env)
    if python is None:
        raise Unsupported("no `python` on the PATH the benchmark would run with")
    probe = run_bounded([python, "-c", "import pyperf"], timeout=timeout, what=f"importing pyperf with {python}",
                        env=env)
    if probe.returncode != 0:
        raise Unsupported(f"pyperf is not importable by {python}: {probe.stderr.strip().splitlines()[-1:]}")
    return {"python": python, "benchmark_dir": benchmark_dir, "root": root,
            "project": _project(benchmark_dir, root), "benchmarks": {}}


def list_cases(runnable, case_filter, env, timeout=None) -> list[str]:
    """The `time_*` benchmarks the filter (a regex, as asv's --bench) selects,
    one case per parameter combination. As in asv, a parametrized benchmark is
    searched once per combination, as `name(p0, p1)`, and only the matching
    combinations run. Modules that fail to import and benchmarks of other types
    are reported on stderr, never run."""
    with tempfile.TemporaryDirectory() as scratch:
        # A copy, so the script's directory on sys.path holds nothing else (adapters/ has a pyperf.py).
        runtime = shutil.copy(RUNTIME, scratch)
        out = run_bounded([runnable["python"], runtime, "list", str(runnable["benchmark_dir"])],
                          timeout=timeout, what=f"listing the asv benchmarks in {runnable['benchmark_dir']}",
                          env=env, cwd=scratch)
    if out.returncode != 0:
        raise Unsupported(f"cannot discover asv benchmarks in {runnable['benchmark_dir']}: "
                          f"{(out.stderr.strip().splitlines() or ['no output'])[-1]}")
    listing = json.loads(out.stdout.strip().splitlines()[-1])
    for failure in listing["failed"]:
        print(f"asv: skipped module {failure['module']}: {failure['error']}", file=sys.stderr)
    cases, other, unmatched = [], {}, []
    runnable["cases"] = {}  # case id -> (benchmark name, combination index or None)
    for info in listing["benchmarks"]:
        if info["params"]:
            selected = [(f"{info['name']}-{i}", (info["name"], i))
                        for i, combo in enumerate(itertools.product(*info["params"]))
                        if not case_filter or re.search(case_filter, f"{info['name']}({', '.join(combo)})")]
            if not selected and case_filter and re.search(case_filter, info["name"]):
                unmatched.append(info["name"])
        else:
            matches = not case_filter or re.search(case_filter, info["name"])
            selected = [(info["name"], (info["name"], None))] if matches else []
        if not selected:
            continue
        if info["type"] != "time":
            other[info["type"]] = other.get(info["type"], 0) + 1
            continue
        scope = info["declared"].get("setup_scope", "call")
        if scope not in SETUP_SCOPES:
            print(f"asv: skipped {info['name']}: setup_scope must be one of {', '.join(map(repr, SETUP_SCOPES))}, "
                  f"not {scope!r}", file=sys.stderr)
            continue
        runnable["benchmarks"][info["name"]] = info
        for case, target in selected:
            if runnable["cases"].setdefault(case, target) != target:
                raise Unsupported(f"asv benchmarks {_describe(runnable['cases'][case])} and {_describe(target)} "
                                  f"share the case id {case!r}; give one of them another benchmark_name")
            cases.append(case)
    not_run = ", ".join(f"{n} {t}" for t, n in sorted(other.items()))
    if not cases:
        notes = []
        if unmatched:
            notes.append(f"the filter matches the name of a parametrized benchmark ({', '.join(unmatched[:3])}) but "
                         "not its arguments, and those are matched as name(p0, p1)")
        if other:
            notes.append(f"only benchmarks that are not run were selected (only time benchmarks map to wall-time): "
                         f"{not_run}")
        if listing["failed"]:
            notes.append(f"{len(listing['failed'])} module(s) failed to import")
        raise no_cases(runnable["benchmark_dir"], case_filter,
                       {i["name"] for i in listing["benchmarks"] if i["type"] == "time"}, notes)
    if other:
        print(f"asv: not run (only time benchmarks map to wall-time): {not_run}", file=sys.stderr)
    return cases


def _describe(target: tuple[str, int | None]) -> str:
    name, index = target
    return repr(name) if index is None else f"{name!r} (combination {index})"


def workload(case: str, runnable: dict) -> tuple[str, dict]:
    """The benchmark name, and its parameters as asv's repr() strings, by name. A case
    id is `name-INDEX` for a parametrized benchmark, which a plain benchmark's own name
    can look like, so the benchmark is found by the table `list_cases` made, not by
    taking the id apart."""
    name, index = runnable["cases"][case]
    info = runnable["benchmarks"][name]
    if index is None:
        return name, {}
    combo = list(itertools.product(*info["params"]))[index]
    return name, dict(zip(info["param_names"], combo))


def _plan(info: dict, invocation: dict) -> dict:
    """The pyperf settings one case runs with: the order's, then the benchmark's, then asv's defaults."""
    declared = info["declared"]
    values, source = invocation["values"], "order"
    if values is None:
        repeat = declared.get("repeat")
        if isinstance(repeat, int) and repeat > 0:
            values, source = repeat, "repeat"
        elif isinstance(repeat, list) and len(repeat) == 3:
            values, source = int(repeat[0]), "repeat-min"
        else:
            values, source = DEFAULT_VALUES, "default"
    loops, loops_from = invocation["loops"], "order"
    if loops is None and int(declared.get("number", 0)) > 0:
        loops, loops_from = int(declared["number"]), "number"
    scope = declared.get("setup_scope", "call") if info["has_setup"] else None
    if loops is None and scope == "call":
        loops, loops_from = 1, "setup_scope"  # every timed call sees fresh state
    elif loops is None:
        loops_from = None
    min_time = invocation["min_time"] if invocation["min_time"] is not None else declared.get(
        "sample_time", DEFAULT_SAMPLE_TIME)
    warmups = invocation["warmups"]
    if warmups is None:
        warmups = 0 if declared.get("warmup_time") == 0 else 1
    return {"processes": invocation["processes"], "values": values, "values_from": source, "loops": loops,
            "min_time": min_time, "warmups": warmups, "timeout": float(declared.get("timeout", DEFAULT_TIMEOUT)),
            "loops_from": loops_from, "setup_scope": scope}


def _flags(plan: dict) -> list[str]:
    flags = ["--processes", str(plan["processes"]), "--values", str(plan["values"]),
             "--warmups", str(plan["warmups"]), "--timeout", str(math.ceil(plan["timeout"])), "--copy-env"]
    if plan["loops"] is not None:
        return flags + ["--loops", str(plan["loops"])]
    return flags + ["--min-time", str(plan["min_time"])]


DRIVER = '''\
# Generated by benchx. Runs one asv benchmark case under pyperf.
import hashlib, json, os, sys
from pathlib import Path

# Before anything of the user's is imported: what this process was given.
_ENV = dict(os.environ)
_DIGEST = hashlib.sha256(
    json.dumps(_ENV, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

import importlib.metadata, importlib.util, pickle
import pyperf

spec = importlib.util.spec_from_file_location("benchx_asv_runtime", {runtime!r})
rt = importlib.util.module_from_spec(spec)
sys.modules["benchx_asv_runtime"] = rt
spec.loader.exec_module(rt)

runner = pyperf.Runner()
runner.parse_args()
runner.metadata[{digest_key!r}] = _DIGEST

if not runner.args.worker:
    if {has_cache!r}:
        # asv's setup_cache step: once per case, before any worker, in the shared scratch directory.
        bench = rt.load({benchmark_dir!r}, {module!r}, {attr!r}, {index!r})
        Path("cache.pickle").write_bytes(pickle.dumps(bench.prepare_cache()))
    runner.bench_time_func({case!r}, lambda loops: 0.0)  # the manager only spawns workers
    sys.exit(0)

# Only the allowlisted names: the digest above covers the whole environment,
# the file is shared, and CI environments carry secrets.
Path({worker_env!r}).write_text(json.dumps({{k: _ENV[k] for k in {env_names!r} if k in _ENV}}, sort_keys=True, indent=1))
bench = rt.load({benchmark_dir!r}, {module!r}, {attr!r}, {index!r})
if {has_cache!r}:
    bench.set_cache(pickle.loads(Path("cache.pickle").read_bytes()))
try:
    bench.setup()
except rt.Skipped as e:
    Path({skipped!r}).write_text(json.dumps({{"reason": str(e)}}))
    sys.exit(0)

project = {project!r}
if project:
    installed = {{"name": project}}
    try:
        installed["version"] = importlib.metadata.version(project)
    except importlib.metadata.PackageNotFoundError:
        pass
    module = sys.modules.get(project)
    if module is not None:
        installed["path"] = getattr(module, "__file__", None)
        for attr in ("__version__", "__git_version__"):
            if isinstance(getattr(module, attr, None), str):
                installed[attr.strip("_")] = getattr(module, attr)
    Path({facts!r}).write_text(json.dumps({{"project": installed}}))
try:
    runner.bench_time_func({case!r}, bench.time_func())
finally:
    bench.teardown()
'''


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def run_case(runnable, case, invocation, env, timeout, native_path, env_names=None) -> dict:
    """One pyperf run for the case; never raises for harness failures, which become results."""
    if env_names is None:
        env_names = snapshot.env_allowlist(env, ENV_ALLOWLIST)
    name, index = runnable["cases"][case]
    info = runnable["benchmarks"][name]
    plan = _plan(info, invocation)
    stem = native_path.name.removesuffix(".native.json")
    driver = native_path.with_name(f"{stem}.driver.py")
    worker_env = native_path.with_name(f"{stem}.worker-env.json")
    sidecar = native_path.with_name(f"{stem}.asv.json")
    runtime = native_path.with_name("asv_runtime.py")
    shutil.copyfile(RUNTIME, runtime)
    for stale in (native_path, worker_env, sidecar):
        stale.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        skipped, facts = Path(scratch, "skipped.json"), Path(scratch, "facts.json")
        driver.write_text(DRIVER.format(
            runtime=str(runtime.resolve()), digest_key=pyperf.ENV_DIGEST_KEY, worker_env=str(worker_env.resolve()),
            env_names=list(env_names), benchmark_dir=str(runnable["benchmark_dir"]), module=info["module"],
            attr=info["attr"], index=index, case=case, has_cache=bool(info["setup_cache_key"]),
            skipped=str(skipped), facts=str(facts), project=runnable["project"] or ""))
        args = [runnable["python"], str(driver.resolve()), *_flags(plan), "-o", str(native_path.resolve())]
        # Its own process group, so a timeout takes the workers down with the manager.
        proc = subprocess.Popen(args, env=env, cwd=scratch, text=True, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            status = proc.returncode
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            status = None
        skip = _read_json(skipped)
        observed = _read_json(facts) or {}
    native = _read_json(native_path)
    if native is not None:
        native_path.write_text(json.dumps(native, indent=1) + "\n")
    asv = {"benchmark": info, "case": case, "plan": plan, "skipped": skip, **observed}
    sidecar.write_text(json.dumps(asv, indent=1) + "\n")
    artifacts = [("driver", "text/x-python", driver), ("asv-runtime", "text/x-python", runtime),
                 ("asv-case", "application/json", sidecar)]
    if worker_env.exists():
        artifacts.append(("worker-environment", "application/json", worker_env))
    # A worker over the benchmark's own timeout: pyperf's manager gives up with 124.
    worker_timeout = status == PYPERF_TIMEOUT_EXIT and "timed out after" in (stdout or "").lower()
    return {"exit_status": status, "timed_out": status is None or worker_timeout,
            "stdout": stdout or "", "stderr": stderr or "",
            "native": {**(native or {"benchmarks": []}), "benchx_asv": asv}, "artifacts": artifacts,
            "worker_env": _read_json(worker_env), "passed_env_digest": pyperf.env_digest(env)}


def _case_applied(applied: dict, plan: dict, version: str | None = None) -> dict:
    """The protocol one case ran under, in benchx terms."""
    if plan["loops"] is not None:
        calibration = {"mode": "fixed", "n_iterations": plan["loops"]}
    else:
        calibration = {"mode": "adaptive", "minimum_sample_seconds": plan["min_time"]}
    warmup = {"mode": "count", "n_warmup": plan["warmups"]} if plan["warmups"] else {"mode": "none"}
    asv = {k: plan[k] for k in ("values_from", "timeout")}
    if plan["setup_scope"]:
        asv["setup_scope"] = plan["setup_scope"]
    if version:
        asv["benchmark_version"] = version
    return {**applied, "repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": plan["processes"]},
                                                                   {"unit": "value", "n": plan["values"]}]},
            "calibration": calibration, "warmup": warmup, "asv": asv}


def case_protocol(applied: dict, run: dict, case: str) -> dict:
    asv = run["native"]["benchx_asv"]
    return _case_applied(applied, asv["plan"], asv["benchmark"].get("version"))


def attempted(applied: dict) -> int:
    """Order-level; each case's own count comes from its plan (`translate`)."""
    n = 1
    for level in applied["repetitions"]["levels"]:
        n *= level["n"]
    return n


def repetition_levels(applied: dict, run: dict, case: str) -> list[dict]:
    return pyperf.repetition_levels(case_protocol(applied, run, case), run, case)


def translate(case: str, quantities: list[str], run: dict, attempted_: int) -> list[dict]:
    asv = run["native"]["benchx_asv"]
    plan = asv["plan"]
    planned = plan["processes"] * plan["values"]
    if asv.get("skipped") is not None:
        return [{"quantity": QUANTITIES[name][1],
                 "measurement": {"status": "skipped", "reason": "harness.skipped"},
                 "procedure": {"attempted_repetitions": planned, "completed_repetitions": 0},
                 "info": {"message": asv["skipped"]["reason"]}} for name in quantities]
    return pyperf.translate(case, quantities, run, planned)


def harness(info: dict) -> dict:
    return pyperf.harness(info)


def context_facts(native: dict | None) -> tuple[dict, dict]:
    """pyperf's facts; in provenance, the installed project the workers saw (it
    changes with the revision, so it is never a system fact) and the benchmark's
    declared attributes. The benchmark's `version` is in its protocol (`case_protocol`)."""
    native = native or {}
    system, info = pyperf.context_facts(native)
    asv = native.get("benchx_asv", {})
    if asv.get("project"):
        info["installed"] = asv["project"]
    benchmark = asv.get("benchmark")
    if benchmark:
        if benchmark.get("version"):
            info["benchmark_version"] = benchmark["version"]
        if benchmark.get("declared"):
            info["declared"] = benchmark["declared"]
        if benchmark.get("setup_cache_key"):
            info["setup_cache_key"] = benchmark["setup_cache_key"]
    return system, info


def _unbatched(run: dict, case: str) -> bool:
    """Setup before every call kept pyperf from batching calls it would have:
    the order left loops to the benchmark, and a call is shorter than a value
    is meant to take."""
    plan = run["native"]["benchx_asv"]["plan"]
    if plan["setup_scope"] != "call" or plan["loops_from"] == "order":
        return False
    benchmark = pyperf._benchmark(run["native"], case)
    values = [v for r, _ in (pyperf._runs(run["native"], benchmark) if benchmark else []) for v in r.get("values", [])]
    return bool(values) and statistics.median(values) < plan["min_time"]


def observed_environment(run: dict, case: str, env_names) -> tuple[dict, list[str]]:
    facts, warnings = pyperf.observed_environment(run, case, env_names)
    if _unbatched(run, case):
        warnings.append(UNBATCHED)
    return facts, warnings
