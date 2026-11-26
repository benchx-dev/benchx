# Example: benchmarking Python functions with `bx bench`

A runnable example of a benchmark suite. `bench_suite.py` holds five small
functions; `bx bench` runs each one under [pyperf](https://pyperf.readthedocs.io),
writes benchx result files, delivers them to a local store, and prints a table.

## First, install benchx

We install `benchx` with `pyperf`

```
$ python3 -m venv .venv && . .venv/bin/activate    # Python >= 3.11, or any environment you like
$ pip install pyperf benchx[parquet]
```

## Benchmarks

In the simplest case, benchmarks are plain python functions. Here is an example suite
of several small microbenchmarks (here we only show one function out of five in the suite):

```sh
head -20 bench_suite.py
```

```text
"""Example benchmark suite: plain Python functions, one case each.

`bx bench bench_suite.py` runs every function named bench_* or benchmark_*.
Name functions on the command line to run only those:
    bx bench bench_suite.py bench_sorted bench_join

Each function is the timed body; pyperf calls it many times. Do your setup
at module level (it runs once per worker process, outside the timing).
"""

import os

DATA = list(range(2000, 0, -1))
WORDS = [f"word{i}" for i in range(500)]


def bench_sorted():
    sorted(DATA)
```

Additionally, this directory contains an optional configuration file, `benchx.conf.json`,
which collects settings for the benchmarking process. See Sec. 7 below for details.

The benchmark functions are timed bodies: `pyperf` calls each one many times, and does
the calibration (how many calls make up one value) itself. Put setup at module level.


## Run the benchmarks

The main CLI entry point for the `benchx` package is the `bx` executable, which
supports several subcommands. 

### 1. Run all benchmarks with default settings

The first subcommand is `bx bench` which accepts the
name of the suite or a selection of benchmarks:

```bash
bx bench . --run-key example-all --out out/all
```

Here the first argument to `bx bench` is the current directory, and it makes it search
for functions names `bench_` in all python files in the current directory.
Other arguments are optional and are only provided for convenience:
`--out` controls where the output files go (default is `$PWD/out`), and `--run-key`
sets the key for the results of this run in the local store. If not provided, it will
be synthesized.
Specific parameters for running benchmarks (the number of repetitions
and so on) will be taken from the configuration file, `benchx.conf.json`.
The output lists benchmarks and their results:


```text
measuring bench_suite.py on .
precision: 5 processes x 3 values (config: /home/br/temp/bex/benchx.conf.json)
case               median    mean ± sd            min .. max            n   status
-----------------  --------  -------------------  --------------------  --  ---------------------------------------------------
bench_dict_build   21.42 us  21.45 us ± 326.9 ns  20.95 us .. 22.09 us  15  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_env_var      676.5 ns  675.3 ns ± 6.897 ns  663.6 ns .. 685.6 ns  15  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_join         4.296 us  4.302 us ± 27.51 ns  4.268 us .. 4.35 us   15  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_sorted       14.35 us  14.44 us ± 188.6 ns  14.28 us .. 14.92 us  15  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_sum_squares  34.18 us  34.35 us ± 344.5 ns  34.11 us .. 35.29 us  15  success  [dirty-subject-tree, dirty-benchmark-tree]

10 result(s) -> out/all  (order sha256:6990583c3230)
/home/br/temp/bex/out/store.parquet: ingested 10, duplicate 0
```

### 2. Run a single benchmark, with an environment variable and process controls

To run a single benchmark from the suite, use the second optional argument of `bx bench`:

```bash
bx bench . bench_env_var -e EXAMPLE_N=100000 --processes 3 \
  --run-key example-env --out out/env
```
Note how parameters that control benchmark execution (here `--processes`) can supplied
as command line arguments---if provided, they superseded the values from the configuration file.
We also set an environment variable, which the benchmark reads, via the `-e` argument.

```text
measuring bench_suite.py on .
precision: 3 processes x 3 values (config: /home/br/temp/bex/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_env_var [EXAMPLE_N=100000]  456.3 ns  455.2 ns ± 6.209 ns  446.9 ns .. 466.7 ns  9  success  [dirty-subject-tree, dirty-benchmark-tree]

2 result(s) -> out/env  (order sha256:097cdeee8c33)
/home/br/temp/bex/out/store.parquet: ingested 2, duplicate 0
```

### 3. Repeat a run

Here we rerun a benchmark again under a new run key, so its series gets a second point:


```bash
bx bench . bench_join --run-key example-again --out out/again
```

```text
measuring bench_suite.py on .
precision: 5 processes x 3 values (config: /home/br/temp/bex/benchx.conf.json)
case        median    mean ± sd            min .. max            n   status
----------  --------  -------------------  --------------------  --  ---------------------------------------------------
bench_join  4.381 us  4.393 us ± 61.69 ns  4.295 us .. 4.536 us  15  success  [dirty-subject-tree, dirty-benchmark-tree]

2 result(s) -> out/again  (order sha256:7b5034bcad0c)
/home/br/temp/bex/out/store.parquet: ingested 2, duplicate 0
```

Note that using a different `--run-key` is essential, while setting a different `--out` directory
only is not.

### 4. Idempotency conflicts

Attempting to reuse the same run key for a different workload is a user error.
The system will refuse to add the new result to the _local store_ (see Section 5 below):
results of the rerun will not be byte-identical, and the store maintains a one-to-one
correspondence between the run key and the run results. The error will be reported
as _idempotency conflict_:


```bash
# same run-key
bx bench . bench_join --run-key example-again --out out/again2
```

```text
measuring bench_suite.py on .
precision: 5 processes x 3 values (config: /home/br/temp/bex/benchx.conf.json)
case        median    mean ± sd           min .. max            n   status
----------  --------  ------------------  --------------------  --  ---------------------------------------------------
bench_join  4.384 us  4.39 us ± 52.28 ns  4.336 us .. 4.549 us  15  success  [dirty-subject-tree, dirty-benchmark-tree]

2 result(s) -> out/again2  (order sha256:26d4dd076e27)
  rejected example-again_bench_join_wall-time.json: idempotency-conflict — same (producer, ingest_key), different payload
  rejected example-again_bench_join_load-average.json: idempotency-conflict — same (producer, ingest_key), different payload
/home/br/temp/bex/out/store.parquet: ingested 0, duplicate 0, rejected 2 (idempotency-conflict)
```


### 5. The local store

Benchmark results are saved to the local store. `bx head` lists its contents:

```bash
bx head -10
```

```text
/home/br/temp/bex/out/store.parquet: 14 result(s); latest 10, newest first
   13  2026-11-25 10:10:24  example-again  bench_join         load-average  success                                              0.7 1          directory-  local/gonzales
   12  2026-11-25 10:10:24  example-again  bench_join         wall-time     success  [dirty-subject-tree, dirty-benchmark-tree]  4.57613e-06 s  directory-  local/gonzales
   11  2026-11-25 10:10:08  example-env    bench_env_var      load-average  success                                              0.52 1         directory-  local/gonzales
   10  2026-11-25 10:10:08  example-env    bench_env_var      wall-time     success  [dirty-subject-tree, dirty-benchmark-tree]  4.45852e-07 s  directory-  local/gonzales
    9  2026-11-25 10:09:53  example-all    bench_sum_squares  load-average  success                                              0.67 1         directory-  local/gonzales
    8  2026-11-25 10:09:53  example-all    bench_sum_squares  wall-time     success  [dirty-subject-tree, dirty-benchmark-tree]  3.42711e-05 s  directory-  local/gonzales
    7  2026-11-25 10:09:50  example-all    bench_sorted       load-average  success                                              0.64 1         directory-  local/gonzales
    6  2026-11-25 10:09:50  example-all    bench_sorted       wall-time     success  [dirty-subject-tree, dirty-benchmark-tree]  1.44032e-05 s  directory-  local/gonzales
    5  2026-11-25 10:09:48  example-all    bench_join         load-average  success                                              0.64 1         directory-  local/gonzales
    4  2026-11-25 10:09:48  example-all    bench_join         wall-time     success  [dirty-subject-tree, dirty-benchmark-tree]  4.31922e-06 s  directory-  local/gonzales
```

Note that each run has two entries, one per measured quantity (by default: wall time and load average).


### 6. List the series in the store

`bx series` lists the series from the local store. We can also filter by measured quantities.

```bash
bx series --quantity wall-time
```

Note that `bench_env_var` workload has two entries: we ran it with two values of the envirornment variable, setting `-e` changes the environment, which `benchx` tracks:

```text
bx series --quantity wall-time
1ba1bf938012  local/gonzales  bench_dict_build  wall-time [s]  est=benchx/median/v1
c4525d138f93  local/gonzales  bench_env_var  wall-time [s]  est=benchx/median/v1
72bbf2a3e37c  local/gonzales  bench_join  wall-time [s]  est=benchx/median/v1
49318d657b29  local/gonzales  bench_sorted  wall-time [s]  est=benchx/median/v1
5442871527d0  local/gonzales  bench_sum_squares  wall-time [s]  est=benchx/median/v1
6d346fd5a7da  local/gonzales  bench_env_var  wall-time [s]  est=benchx/median/v1
```

To filter by the workload use the `--workload` argument: `bx series --quantity wall-time --workload bench_env_var`.


### 7. The config file

An optional `benchx.conf.json` file collects various configuration settings. Calling
`bx config init` writes a file with default settings, which you can then customize.
A bad config file is reported before anything runs.
Settings provided as command line arguments take precedence over the config file.
Here is an example:

```bash
cat benchx.conf.json
```

```json
{
  "schema_version": "benchx/config/0.1.0",
  "adapter": {
    "name": "pyperf",
    "suite": "bench_suite.py",
    "precision": {
      "repetitions": {
        "mode": "fixed",
        "levels": [{"unit": "process", "n": 5}, {"unit": "value", "n": 3}]
      },
      "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.05},
      "warmup": {"mode": "count", "n_warmup": 1}
    }
  },
  "paths": {"store": "out/store.parquet", "results": "out"}
}
```

Here the suite is being with `pyperf` via its harness adapter; Additional settings are
supplied to `pyperf` through the work-order:

- `repetitions.levels`: `process` is the number of worker processes, `value` the values
  each one records. Leave a level out and it takes pyperf's default (20 and 3).
- `calibration`: `adaptive` with `minimum_sample_seconds` lets pyperf pick how many calls
  make up one value; `{"mode": "fixed", "n_iterations": N}` fixes it.
- `warmup`: `{"mode": "count", "n_warmup": N}` discards N values per process, or `{"mode": "none"}`.
- Every key is optional; pyperf's defaults (20 x 3, adaptive 0.1 s, 1 warmup) fill the rest.

Precision is resolved as: **flags, then the config file, then pyperf's defaults**
(20 processes x 3 values, 0.1 s calibration, 1 warmup). A level you leave out of the
file takes pyperf's default.



### 8. What is written

Finally, take a peek into the output directory (typically, you never need to look there
unless debugging):

```bash
ls -l out/all/
```

```text
drwxrwxr-x 3 br br  4096 ноя 25 11:09 artifacts
-rw-rw-r-- 1 br br 24869 ноя 25 11:09 bx.log
-rw-rw-r-- 1 br br  2799 ноя 25 11:09 example-all_bench_dict_build_load-average.json
-rw-rw-r-- 1 br br  6413 ноя 25 11:09 example-all_bench_dict_build_wall-time.json
-rw-rw-r-- 1 br br  2793 ноя 25 11:09 example-all_bench_env_var_load-average.json
-rw-rw-r-- 1 br br  6394 ноя 25 11:09 example-all_bench_env_var_wall-time.json
-rw-rw-r-- 1 br br  2787 ноя 25 11:09 example-all_bench_join_load-average.json
-rw-rw-r-- 1 br br  6388 ноя 25 11:09 example-all_bench_join_wall-time.json
-rw-rw-r-- 1 br br  2791 ноя 25 11:09 example-all_bench_sorted_load-average.json
-rw-rw-r-- 1 br br  6406 ноя 25 11:09 example-all_bench_sorted_wall-time.json
-rw-rw-r-- 1 br br  2801 ноя 25 11:09 example-all_bench_sum_squares_load-average.json
-rw-rw-r-- 1 br br  6403 ноя 25 11:09 example-all_bench_sum_squares_wall-time.json
drwxrwxr-x 2 br br  4096 ноя 25 11:09 orders
-rw-rw-r-- 1 br br   968 ноя 25 11:09 workorder-7ff9a9ec43dc8960acf3fcaf08a3d4ab4898e204f8bc7d09a2271d276e992980.json
```

`benchx` stores a host of information:

- `bx.log` file is the log file of internal `benchx` operations (the default logging level is DEBUG);
- `example-...json` files contain detailed results from individual runs, which are ingested into the parquet store;
- "work orders" are internal `benchx` way of tracking what to run and how: you can exactly replay a run
with `bx run workorder-file.json`;
- `artifacts/` directory contains full details low-level inputs and outputs of harness adapters.

