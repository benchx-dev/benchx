"""A stand-in for a Google Benchmark binary, for tests without a compiler.

Speaks the part of the Google Benchmark command line the adapter uses and
writes its JSON format. Behaviour comes from fake-bench.json beside it:

  {"seed": 1, "cases": {"BM_Quiet/1024": {"mean": 1e-3, "rel_sd": 0.002}},
   "error": ["BM_Broken"], "skip": ["BM_Skipped"], "sleep": {"BM_Slow": 5},
   "ghost": ["BM_Ghost"]}

A ghost case is listed but never reported, as a binary that drops a case does.

Each invocation draws fresh noise, seeded by the seed, the case, and a
counter kept beside the binary, so a run is reproducible from a clean state.
"""

import json
import random
import re
import sys
import time
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
config = json.loads((here / "fake-bench.json").read_text())
args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
pattern = re.compile(args.get("--benchmark_filter", "."))
cases = [c for c in list(config["cases"]) + config.get("error", []) + config.get("skip", [])
         + list(config.get("sleep", {})) if pattern.search(c)]

if args.get("--benchmark_list_tests") == "true":
    print("\n".join(cases + [c for c in config.get("ghost", []) if pattern.search(c)]))
    sys.exit(0)

counter_file = here / "fake-bench.counter"
counter = int(counter_file.read_text()) if counter_file.exists() else 0
counter_file.write_text(str(counter + 1))
repetitions = int(args.get("--benchmark_repetitions", "1"))

rows = []
for case in cases:
    if case in config.get("sleep", {}):
        time.sleep(config["sleep"][case])
    if case in config.get("error", []):
        rows.append({"name": case, "run_name": case, "run_type": "iteration", "repetitions": repetitions,
                     "repetition_index": 0, "error_occurred": True, "error_message": "fake failure",
                     "iterations": 0, "real_time": 0, "cpu_time": 0, "time_unit": "ns"})
        continue
    if case in config.get("skip", []):
        rows.append({"name": case, "run_name": case, "run_type": "iteration", "repetitions": repetitions,
                     "repetition_index": 0, "skipped": True, "skip_message": "fake skip",
                     "iterations": 0, "real_time": 0, "cpu_time": 0, "time_unit": "ns"})
        continue
    spec = config["cases"][case]
    rng = random.Random(f"{config.get('seed', 0)}:{case}:{counter}")
    values = [spec["mean"] * (1 + rng.gauss(0, spec["rel_sd"])) for _ in range(repetitions)]
    for i, v in enumerate(values):
        rows.append({"name": case, "run_name": case, "run_type": "iteration", "repetitions": repetitions,
                     "repetition_index": i, "iterations": 1000, "real_time": v * 1e9,
                     "cpu_time": v * 0.99e9, "time_unit": "ns"})
    if repetitions > 1:
        mean = sum(values) / len(values)
        for name, value in (("mean", mean), ("median", sorted(values)[len(values) // 2])):
            rows.append({"name": f"{case}_{name}", "run_name": case, "run_type": "aggregate",
                         "aggregate_name": name, "aggregate_unit": "time", "iterations": repetitions,
                         "real_time": value * 1e9, "cpu_time": value * 0.99e9, "time_unit": "ns"})

native = {"context": {"date": "2026-09-25T10:00:00+00:00", "host_name": "fake-host", "num_cpus": 8,
                      "mhz_per_cpu": 3000, "cpu_scaling_enabled": False, "library_build_type": "release",
                      "library_version": "v1.9.1-fake", "executable": sys.argv[0]},
          "benchmarks": rows}
if "--benchmark_out" in args:
    Path(args["--benchmark_out"]).write_text(json.dumps(native))
print("\n".join(r["name"] for r in rows))
