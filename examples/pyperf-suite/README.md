# Example: benchmarking Python functions with `bx bench`

A runnable example of a benchmark suite. `bench_suite.py` holds five small
functions; `bx bench` runs each one under [pyperf](https://pyperf.readthedocs.io),
writes benchx result files, delivers them to a local store, and prints a table.

## First, install benchx

We install `benchx` with `pyperf`

```
$ python3 -m venv .venv && . .venv/bin/activate    # Python >= 3.11, or any environment you like
$ pip install pyperf benchx
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

Additionally, this directory contains two additional files: `benchx.conf.json`
is an optional configuration file, and `run.sh` is a helper script which demonstrates
basic CLI commands.

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
be synthesized. Specific parameters for running benchmarks (the number of repetitions
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

### 4. The local store

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


### 5. List the series in the store

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


### What is written

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




Here we demonstrate several `bx` subcommands, in a
shell script (see below for specific steps the script):


```sh
bash ./run.sh
```

The script runs `bx` and `python` from the active environment, so `benchx` and `pyperf`
must be installed there; the workers use that same `python`. Extra arguments go to
`bx bench` in step 1 and win over the config file, e.g. `./run.sh --processes 20`.
On a particular machine, the run takes about 20 seconds.
Nothing outside the root directory changes: the local store is `out/store.parquet`
(`paths.store` in `benchx.conf.json`).

## What goes in

| Input | Where | What it does |
|---|---|---|
| `.` | first argument | The target: this directory, a working tree. pyperf reads a bare path that way. |
| `benchx.conf.json` | found by walking up from where you run `bx`; its directory is the project root | The harness (`adapter.name`), the module (`adapter.suite`, `bench_suite.py`: each function named `bench_*` or `benchmark_*` is a case), and the precision: worker processes, values per process, calibration, warmup. Here: 5 processes x 3 values, calibrate to 0.05 s per value. |
| Function names | after the target | `bx bench . bench_sorted bench_join` runs only those, any function, whatever its name. `bx bench bench_suite.py bench_sorted` still works as a shorthand: the module's git checkout is the target and its path the suite. |
| Flags | command line | `--processes`, `--values`, `--min-time`, `--loops`, `--warmups` override the file, and so does `--suite`. |
| `-e NAME=VALUE` | command line | Sets an environment variable for the workers. The workers otherwise inherit your whole environment. |

Precision is resolved as: **flags, then the config file, then pyperf's defaults**
(20 processes x 3 values, 0.1 s calibration, 1 warmup). A level you leave out of the
file takes pyperf's default. `bx config init` writes a `benchx.conf.json` that states
the defaults, as a template to edit, and `bx config show` says where each value comes from. The resolved values are written into the work order,
so a result records the precision that applied. A bad config file is reported before
anything runs.

The benchmark functions are timed bodies: pyperf calls each one many times, and does
the calibration (how many calls make up one value) itself. Put setup at module level.

## The config file

`benchx.conf.json` is the example's project file. Its `adapter.precision` is the work-order
`precision` block; `adapter.name` and `adapter.suite` say which harness and which module:

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

- `repetitions.levels`: `process` is the number of worker processes, `value` the values
  each one records. Leave a level out and it takes pyperf's default (20 and 3).
- `calibration`: `adaptive` with `minimum_sample_seconds` lets pyperf pick how many calls
  make up one value; `{"mode": "fixed", "n_iterations": N}` fixes it.
- `warmup`: `{"mode": "count", "n_warmup": N}` discards N values per process, or `{"mode": "none"}`.
- Every key is optional; pyperf's defaults (20 x 3, adaptive 0.1 s, 1 warmup) fill the rest.
  Run `bx config init` in your own project to get a file that states them all.

## What `run.sh` does

1. `bx bench .` runs all five cases with the config file's suite and precision.
2. `bx bench . bench_env_var -e EXAMPLE_N=100000 --processes 3 --values 2`
   runs one case, sets a variable for the workers, and overrides the file's process count.
3. `bx bench . bench_join --run-key example-again` runs one case a second time
   under a new run key, so its series gets a second point.
4. `ls -l` and `bx head` show the local store: one Parquet file, and its latest rows.
5. `bx series` lists the series in the store.
6. `bx history` lists the points of the `bench_join` series, one per run. The script fails
   if there are not two, which is what CI relies on.
7. It lists the files written.

## What comes out

The table, for step 1 (your numbers will differ):

```
case               median    mean ± sd            min .. max            n   status
-----------------  --------  -------------------  --------------------  --  -------
bench_dict_build   18.29 us  18.56 us ± 680.9 ns  17.71 us .. 19.83 us  15  success
bench_join         3.585 us  3.543 us ± 272.4 ns  3.236 us .. 3.997 us  15  success
```

`n` is processes x values (5 x 3 = 15). Each value is the time of one call, in seconds
underneath. A `[warning]` after the status means the result carries a quality warning,
such as `env-mismatch`.

Files, under `out/all/`, `out/env/` and `out/again/` (one directory for each of the first
three `bx bench` calls in `run.sh`):

- `<run-key>_<case>_wall-time.json`: one benchx result per case (schema `measurement-result`;
  with `-e` the name also carries the percent-encoded parameters, as in
  `example-env_bench_env_var_%7B%22EXAMPLE_N%22%3A%22100000%22%7D_wall-time.json`):
  the observations grouped by worker process, the pyperf protocol that applied, the
  environment, the revision of the tree, and warnings.
- `<run-key>_<case>_load-average.json`: beside each case's result, the 1-minute load
  average the runner read before the case ran, a condition measurement with the same
  `attempt_key` (schema §4.4). `bx series` lists its series too; `--quantity wall-time`
  leaves them out.
- `workorder-<hash>.json`: the work order that was run, and `orders/order.json`, the
  same order as written for you to read. Results refer to it by hash.
- `artifacts/<hash>/NNNN.native.json`: pyperf's own output; `.driver.py`: the generated
  script that ran the function; `.worker-env.json`: the environment a worker really had;
  `.stdout` and `.stderr`.

The local store, `out/store.parquet`, holds the results. `run.sh` reads it back, and
keeps what `bx series` and `bx history` print in `out/series.txt`, `out/join-series.txt`
and `out/history.txt` (your numbers and fingerprints will differ):

```
$ bx series --quantity wall-time
d780c9564c72  local/be8372c04606  bench_dict_build  wall-time [s]  est=benchx/median/v1
e6eaa02c8309  local/be8372c04606  bench_env_var  wall-time [s]  est=benchx/median/v1
4be59ef6debc  local/be8372c04606  bench_join  wall-time [s]  est=benchx/median/v1
...
85a6aa9c6996  local/be8372c04606  bench_env_var  wall-time [s]  est=benchx/median/v1

$ bx history 4be59ef6debc
revision order unavailable without a revision graph; listed by measurement start
d83d37b69010  2026-10-13T11:04:28.656515Z  3.32107e-06 (server)  example-all:bench_join:wall-time
d83d37b69010  2026-10-13T11:04:39.045277Z  3.76987e-06 (server)  example-again:bench_join:wall-time
```

`bench_env_var` has two series because step 2 sets `EXAMPLE_N`, which changes the
environment the case ran in. `bench_join` has one series with two points, one for each
run key. You can also `bx ingest` more result files; comparing two runs is not part of
this step.

## Notes

- The tree measured is the git checkout containing the module (here, this repository), as
  found, uncommitted changes included. The result records the revision and whether the tree
  was dirty. Because that tree is the whole repository, an uncommitted change anywhere in it,
  in a doc or another example as much as in `bench_suite.py`, makes the result `dirty` and
  changes its tree id; commit or stash first for a clean result. Outside git, it records a hash of the directory's contents. `--tree DIR` picks
  the tree.
- pyperf prints advice when the machine is noisy; it is in `.stderr`. On a laptop,
  close other programs. `python -m pyperf system tune` (needs root) quiets the CPU further.
