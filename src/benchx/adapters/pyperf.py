"""pyperf adapter (harness-adapter.md §6.8), both halves.

Driving: the suite is a Python module inside the target; a case is one of its
functions. Each case gets a generated driver script (kept as an artifact) that
runs `Runner.bench_func` in a child interpreter resolved from the child's own
PATH. pyperf must be importable there, or the order is refused. Translating:
one case's pyperf JSON (`-o`) to one measurement and procedure.

Parameters: pyperf has none of its own, so a function declares them as asv does,
with the attributes `params` (one list of values per parameter, or one flat list)
and `param_names`. Each combination is a case, `function-INDEX`, and the driver
calls the function with that combination as its positional arguments. The values
are recorded as `workload.parameters`, in the strings `benchx_params` makes of
them (asv's repr() strings). A filter is searched in `function(p0, p1)` for such
a function, so its bare name selects every combination (`benchx_params.expand`).
`setup`, `teardown` and `skip_params` are asv's, and pyperf's `bench_func` has no
place for them.

Version: a function has one, as in asv, so that an edited benchmark is not compared with its
old self. It is the function's `version` attribute when it has one, else a hash of its source
(`benchx_params.source_version`), found when the module is listed, and recorded in each
result's `protocol.pyperf.benchmark_version`. Protocol is what the sides must agree on, so two
revisions whose function differs fail `sides-agree`. A function without readable source has no
version.

The driver records the environment it actually had, before it imports the
user's module: a digest in every worker's run metadata and, from a worker,
the full environment as an artifact. The adapter reports what the workers had, not what the manager
passed, and warns `env-mismatch` when the two differ.
"""

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

from .. import snapshot
from . import benchx_params
from .base import (
    Unsupported,
    check_filter,
    kill_group,
    last_json,
    name_only_note,
    no_cases,
    note_case,
    note_case_ended,
    note_cases,
    note_parameters,
    parametrized_workload,
    run_bounded,
    stop_group,
)

logger = logging.getLogger(__name__)

NAME = "pyperf"
# The suite is a Python source file, which may live outside the subject's checkout.
SUITE_IS_SOURCE = True
CONTEXT_KEY = "pyperf"
WORKLOAD_PARAMETERS = False
PRODUCER = {"name": "benchx/pyperf-adapter", "version": "0.2.0", "mapping_version": "pyperf-to-benchx/v1"}

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

PARAMS = Path(__file__).with_name("benchx_params.py")  # the driver loads a copy of it by path

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
        problems.append(
            "pyperf repeats at two levels; give repetitions as "
            "{mode: fixed, levels: [{unit: process, n}, {unit: value, n}]}"
        )
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
        "repetitions": {
            "mode": "fixed",
            "levels": [{"unit": "process", "n": processes}, {"unit": "value", "n": values}],
        },
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


def locate(target_path, suite, env, timeout=None) -> dict:
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
    probe = run_bounded(
        [python, "-c", "import pyperf"], timeout=timeout, what=f"importing pyperf with {python}", env=env
    )
    if probe.returncode != 0:
        raise Unsupported(
            f"pyperf is not importable by {python}: {(probe.stderr.strip().splitlines() or ['no output'])[-1]}"
        )
    return {"python": python, "module": module.resolve(), "root": root.resolve()}


LISTER = r"""
import importlib.util, json, os, sys
module_path, params_path = sys.argv[1], sys.argv[2]
sys.path[:0] = [os.path.dirname(module_path), os.getcwd()]
spec = importlib.util.spec_from_file_location("benchx_params", params_path)
params = importlib.util.module_from_spec(spec)
sys.modules["benchx_params"] = params
spec.loader.exec_module(params)
spec = importlib.util.spec_from_file_location("benchx_suite", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules["benchx_suite"] = module
spec.loader.exec_module(module)
found = []
for name, func in sorted(vars(module).items()):
    if callable(func) and getattr(func, "__module__", None) == "benchx_suite" and not name.startswith("_"):
        info = {"name": name, "params": [], "param_names": []}
        try:
            _, info["param_names"], info["params"] = params.normalize(
                getattr(func, "params", []), getattr(func, "param_names", []))
        except Exception as error:
            info["error"] = f"{type(error).__name__}: {error}"
        try:
            version = params.source_version(func)
        except Exception:  # a callable with odd attributes: no version, still a benchmark
            version = None
        if version is not None:
            info["version"] = version
        found.append(info)
print(json.dumps(found))
"""


def list_cases(runnable, case_filter, env, timeout=None) -> list[str]:
    """The module's functions the filter selects: with no filter those named `bench_*` or
    `benchmark_*`, with one any function, by a regex searched in the name (and, for a
    function with `params`, in `name(p0, p1)`, once per combination; see
    `benchx_params.expand`). A combination is the case `function-INDEX`."""
    check_filter(case_filter)
    try:
        where = runnable["module"].relative_to(runnable["root"])
    except ValueError:  # a suite outside the target, named by its absolute path
        where = runnable["module"]
    out = run_bounded(
        [runnable["python"], "-c", LISTER, str(runnable["module"]), str(PARAMS)],
        timeout=timeout,
        what=f"importing {runnable['module'].name} to list its benchmarks",
        env=env,
        cwd=runnable["root"],
        scripts={"LISTER": LISTER},
    )
    if out.returncode != 0:
        raise Unsupported(
            f"cannot import {runnable['module'].name}: {(out.stderr.strip().splitlines() or ['no output'])[-1]}"
        )
    listing = last_json(out, runnable["module"].name)
    runnable["benchmarks"], runnable["cases"] = {}, {}  # see base.parametrized_workload
    cases, unmatched, left_out, unversioned = [], [], 0, []
    for info in listing:
        name = info["name"]
        if not case_filter and not name.startswith(DEFAULT_CASE_PREFIXES):
            left_out += 1
            continue
        selected = benchx_params.expand(name, info["params"], case_filter)
        if not selected:
            if "error" in info:  # not selected, so not refused: the log is the only place that says so
                logger.debug("%s: its params could not be read, and it was not selected: %s", name, info["error"])
            if benchx_params.matches_name_only(name, info["params"], case_filter):
                unmatched.append(info)
            continue
        if "error" in info:
            raise Unsupported(f"cannot read the params of {name} in {where}: {info['error']}")
        if info.get("version") is None:
            unversioned.append(name)
        runnable["benchmarks"][name] = info
        for case, index in selected:
            runnable["cases"][case] = (name, index)
            cases.append(case)
    if left_out:
        logger.debug(
            "with no filter only bench_* and benchmark_* functions are cases: %d other callable(s) of %s were left out",
            left_out,
            where,
        )
    if unversioned:  # no `version` attribute, and its source cannot be read: no benchmark_version in its results
        logger.debug(
            "no version for %s: an edit to it starts no new series and is not caught as a failed `sides-agree`",
            ", ".join(unversioned[:40]),
        )
    if unmatched:
        logger.debug(
            "a filter that matches the name but not the arguments of %d parametrized benchmark(s): %s",
            len(unmatched),
            ", ".join(info["name"] for info in unmatched[:3]),
        )
    if not cases:
        defaults = [info["name"] for info in listing if info["name"].startswith(DEFAULT_CASE_PREFIXES)]
        raise no_cases(
            where,
            case_filter,
            defaults or [info["name"] for info in listing],
            [name_only_note(unmatched)] if unmatched else [],
        )
    note_parameters(logger, runnable)
    return note_cases(logger, cases, case_filter)


workload = parametrized_workload  # the function, and its parameters as value strings


# The combination index is bound to a name before it is tested: `0 is not None` is a SyntaxWarning, which
# Python prints on the stderr of every worker.
DRIVER = """\
# Generated by benchx. Runs one function under pyperf: one case, which for a function
# that declares params is one combination of them, passed as its arguments.
import hashlib, json, os, sys
from pathlib import Path

# Before anything of the user's is imported: what this process was given.
_ENV = dict(os.environ)
_DIGEST = hashlib.sha256(
    json.dumps(_ENV, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

import importlib.util
import pyperf

spec = importlib.util.spec_from_file_location("benchx_params", {params!r})
params = importlib.util.module_from_spec(spec)
sys.modules["benchx_params"] = params
spec.loader.exec_module(params)

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
func = getattr(module, {function!r})
index = {index!r}
args = ()
if index is not None:
    values, _, _ = params.normalize(getattr(func, "params", []), getattr(func, "param_names", []))
    args = params.combination(values, index)
runner.bench_func({case!r}, func, *args)
"""


def run_case(runnable, case, flags, env, timeout, native_path, env_names=None, harness_verbosity=0) -> dict:
    """Run one case; never raises for harness failures, which become results. A `harness_verbosity` above 0
    asks pyperf for `--verbose`: its workers then print a line after every value they measure."""
    if env_names is None:
        env_names = snapshot.env_allowlist(env, ENV_ALLOWLIST)
    stem = native_path.name.removesuffix(".native.json")
    driver = native_path.with_name(f"{stem}.driver.py")
    worker_env = native_path.with_name(f"{stem}.worker-env.json")
    params = native_path.with_name(PARAMS.name)
    for stale in (native_path, worker_env):
        stale.unlink(missing_ok=True)
    shutil.copyfile(PARAMS, params)
    function, index = runnable.get("cases", {}).get(case, (case, None))  # a case run without listing is a function
    driver.write_text(
        DRIVER.format(
            digest_key=ENV_DIGEST_KEY,
            worker_env=str(worker_env.resolve()),
            env_names=list(env_names),
            module=str(runnable["module"]),
            module_dir=str(runnable["module"].parent),
            root=str(runnable["root"]),
            case=case,
            function=function,
            index=index,
            params=str(params.resolve()),
        )
    )
    # Absolute: the child runs in the tree, not where the output directory was named.
    args = [
        runnable["python"],
        str(driver.resolve()),
        *flags,
        *(["--verbose"] if harness_verbosity else []),
        "-o",
        str(native_path.resolve()),
    ]
    # Its own process group, so a timeout takes the workers down with the manager.
    note_case(logger, args, cwd=runnable["root"], timeout=timeout)
    proc = subprocess.Popen(
        args,
        env=env,
        cwd=runnable["root"],
        text=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        status = proc.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr = kill_group(proc)
        status = None
    except KeyboardInterrupt:
        stop_group(proc)  # the group is a session of its own: the terminal's Ctrl-C did not reach it
        raise
    native = None
    if native_path.exists():
        try:
            native = json.loads(native_path.read_text())
            native_path.write_text(json.dumps(native, indent=1) + "\n")
        except ValueError:
            native = None
    note_case_ended(logger, status, timeout, native_path, parsed=native is not None)
    captured = None
    if worker_env.exists():
        try:
            captured = json.loads(worker_env.read_text())
        except ValueError:
            captured = None
    artifacts = [("driver", "text/x-python", driver), ("params-helper", "text/x-python", params)]
    if worker_env.exists():
        artifacts.append(("worker-environment", "application/json", worker_env))
    return {
        "exit_status": status,
        "timed_out": status is None,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "native": native,
        "artifacts": artifacts,
        "worker_env": captured,
        "passed_env_digest": env_digest(env),
        "benchmark_version": runnable.get("benchmarks", {}).get(function, {}).get("version"),
    }


def _failure_message(stderr: str) -> str:
    """The worker's exception line: the line that ends a traceback (the first unindented line after
    "Traceback"), the last one when exceptions were chained. A warning printed before is not it, and
    neither is the manager's own "<python> failed with exit code N" after a second traceback: that wrapper
    only counts when nothing else was said. Without a traceback (a killed process), the first line said."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    ended, in_traceback = [], False
    for line in lines:
        if line.startswith("Traceback"):
            in_traceback = True
        elif in_traceback and not line[0].isspace():
            ended.append(line)
            in_traceback = False
    own = [line for line in ended if "failed with exit code" not in line]
    if own or ended:
        return (own or ended)[-1]
    return next((line for line in lines if not line[0].isspace()), lines[-1] if lines else stderr.strip())


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


def case_protocol(applied: dict, run: dict, case: str) -> dict:
    """One case's protocol: the order's, and the version of the benchmark function when there is one
    (`benchx_params.source_version`), so that sides that ran different code do not agree."""
    version = run.get("benchmark_version")
    return {**applied, "pyperf": {"benchmark_version": version}} if version else applied


def harness(info: dict) -> dict:
    out = {"name": NAME}
    if info.get("library_version"):
        out["version"] = info["library_version"]
    return out


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
    calibration = [
        (r, md)
        for r, md in runs
        if not r.get("values") and ("calibrate_loops" in r["metadata"] or "recalibrate_loops" in r["metadata"])
    ]
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
                info["inner_iterations_per_run"] = [md.get("loops", 1) * md.get("inner_loops", 1) for _, md in measured]
            procedure["warmups_performed"] = sum(len(r.get("warmups", [])) for r, _ in measured)
            if calibration:
                procedure["calibration_runs"] = len(calibration)
        if run["exit_status"] not in (0, None):
            info["exit_status"] = run["exit_status"]
        outputs.append({"quantity": quantity, "measurement": measurement, "procedure": procedure, "info": info})
    return outputs


# File-level pyperf metadata as environment.system facts (schema Appendix B):
# {metadata key: (system object, key)}. The default identity policy ignores
# cpu and os and matches runtime. boot_time is provenance, not a system fact:
# it says when the machine started, and /proc/stat's btime drifts (WSL2), so
# as a fact it would split one environment into several (#121).
_SYSTEM = {
    "cpu_config": ("cpu", "config"),
    "cpu_affinity": ("cpu", "affinity"),
    "aslr": ("os", "aslr"),
    "platform": ("os", "platform"),
    "python_version": ("runtime", "python"),
    "python_implementation": ("runtime", "implementation"),
    "python_compiler": ("runtime", "compiler"),
    "python_cflags": ("runtime", "cflags"),
    "python_gc": ("runtime", "gc"),
}


def context_facts(native: dict | None) -> tuple[dict, dict]:
    """The native metadata split by meaning: (environment.system facts, provenance info).

    The file-level metadata: what held for every run. Facts that differ per
    run (load, frequency, temperature) stay in the native artifact.
    """
    md = (native or {}).get("metadata", {})
    system = {}
    for key, (group, name) in _SYSTEM.items():
        if key in md:
            system.setdefault(group, {})[name] = md[key]
    info = {k: md[k] for k in ("hostname", "cpu_model_name", "cpu_count", "python_executable", "boot_time") if k in md}
    if "perf_version" in md:
        info["library_version"] = md["perf_version"]
    return system, info


def observed_environment(run: dict, case: str, env_names) -> tuple[dict, list[str]]:
    """The environment the workers actually had, and the warnings about it.

    Facts come from the worker-side capture, never from what the manager
    passed. A digest in every run is compared with the digest of what the
    manager passed; a mismatch or a missing digest is `env-mismatch`. When
    nothing was captured, the process environment is null (read but
    undeterminable, so it matches nothing), never substituted by the manager's.
    """
    warnings = []
    captured = run.get("worker_env")
    facts = {"process": {"environment": None}}
    if captured is not None:
        facts["process"]["environment"] = {n: captured[n] for n in env_names if n in captured}
    native = run["native"]
    benchmark = _benchmark(native, case)
    if benchmark is not None:
        digests = {md.get(ENV_DIGEST_KEY) for r, md in _runs(native, benchmark) if r.get("values")}
        if digests != {run["passed_env_digest"]}:
            warnings.append("env-mismatch")
        if captured is None:
            warnings.append("env-capture-missing")
    return facts, warnings


def explain_warnings(run: dict, case: str, warnings: list[str]) -> dict[str, str]:
    """The reason behind each of `observed_environment`'s warnings, for the case's line in the log."""
    details = {}
    native = run["native"]
    benchmark = _benchmark(native, case)
    if "env-mismatch" in warnings and benchmark is not None:
        digests = [md.get(ENV_DIGEST_KEY) for r, md in _runs(native, benchmark) if r.get("values")]
        matched = sum(1 for digest in digests if digest == run["passed_env_digest"])
        details["env-mismatch"] = f"{matched} of {len(digests)} worker(s) had the environment the manager passed"
    if "env-capture-missing" in warnings:
        written = any(kind == "worker-environment" for kind, _, _ in run.get("artifacts", []))
        details["env-capture-missing"] = (
            "the worker-environment file did not parse" if written else "no worker-environment file was written"
        )
    return details


def default_precision() -> dict:
    """pyperf's own defaults, spelled as a work-order `precision`."""
    return {
        "repetitions": {
            "mode": "fixed",
            "levels": [{"unit": "process", "n": DEFAULTS["processes"]}, {"unit": "value", "n": DEFAULTS["values"]}],
        },
        "calibration": dict(DEFAULTS["calibration"]),
        "warmup": dict(DEFAULTS["warmup"]),
    }
