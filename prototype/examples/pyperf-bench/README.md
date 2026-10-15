# Example: benchmarking Python functions with `bx bench`

A runnable example of a benchmark suite. `bench_suite.py` holds five small
functions; `bx bench` runs each one under [pyperf](https://pyperf.readthedocs.io),
writes benchx result files, delivers them to a local store, and prints a table.

## Run it

```sh
cd prototype
python3 -m venv .venv && . .venv/bin/activate    # Python >= 3.11, or any environment you like
pip install -e . pyperf
examples/pyperf-bench/run.sh
```

The script runs `bx` and `python` from the active environment, so benchx and pyperf must
be installed there; the workers use that same `python`. Extra arguments go to
`bx bench` in step 1 and win over the config file, e.g. `./run.sh --processes 20`.
It takes about 20 seconds. Nothing outside this directory changes: the local store is
`out/home/store.parquet` (the script sets `BENCHX_HOME`), and `out/` is deleted at
the start of each run.

## What goes in

| Input | Where | What it does |
|---|---|---|
| `bench_suite.py` | first argument | The module. Each function named `bench_*` or `benchmark_*` is a case. |
| Function names | after the module | `bx bench bench_suite.py bench_sorted bench_join` runs only those, any function, whatever its name. |
| `.benchx/config.json` | found by walking up from where you run `bx` | Precision: worker processes, values per process, calibration, warmup. Here: 5 processes x 3 values, calibrate to 0.05 s per value. |
| Flags | command line | `--processes`, `--values`, `--min-time`, `--loops`, `--warmups` override the file. |
| `-e NAME=VALUE` | command line | Sets an environment variable for the workers. The workers otherwise inherit your whole environment. |

Precision is resolved as: **flags, then the config file, then pyperf's defaults**
(20 processes x 3 values, 0.1 s calibration, 1 warmup). A level you leave out of the
file takes pyperf's default. `bx config init` writes a `.benchx/config.json` that states
the defaults, as a template to edit. The resolved values are written into the work order,
so a result records the precision that applied. A bad config file is reported before
anything runs.

The benchmark functions are timed bodies: pyperf calls each one many times, and does
the calibration (how many calls make up one value) itself. Put setup at module level.

## The config file

`.benchx/config.json` (a hidden directory: use `ls -a`) is the example's precision config.
It is the work-order `precision` block under the adapter's name:

```json
{
  "adapters": {
    "pyperf": {
      "precision": {
        "repetitions": {
          "mode": "fixed",
          "levels": [{"unit": "process", "n": 5}, {"unit": "value", "n": 3}]
        },
        "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.05},
        "warmup": {"mode": "count", "n_warmup": 1}
      }
    }
  }
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

1. `bx bench bench_suite.py` runs all five cases with the config file's precision.
2. `bx bench bench_suite.py bench_env_var -e EXAMPLE_N=100000 --processes 3 --values 2`
   runs one case, sets a variable for the workers, and overrides the file's process count.
3. `bx bench bench_suite.py bench_join --run-key example-again` runs one case a second time
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

Files, under `out/all/` and `out/env/`:

- `<run-key>_<case>_wall-time.json`: one benchx result per case (schema `measurement-result`;
  with `-e` the name also carries the percent-encoded parameters, as in
  `example-env_bench_env_var_%7B%22EXAMPLE_N%22%3A%22100000%22%7D_wall-time.json`):
  the observations grouped by worker process, the pyperf protocol that applied, the
  environment, the revision of the tree, and warnings.
- `workorder-<hash>.json`: the work order that was run, and `orders/order.json`, the
  same order as written for you to read. Results refer to it by hash.
- `artifacts/<hash>/NNNN.native.json`: pyperf's own output; `.driver.py`: the generated
  script that ran the function; `.worker-env.json`: the environment a worker really had;
  `.stdout` and `.stderr`.

The local store, `out/home/store.parquet`, holds the results. `run.sh` reads it back
(your numbers and fingerprints will differ):

```
$ bx series
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
