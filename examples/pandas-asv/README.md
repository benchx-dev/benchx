# pandas-asv: one pandas asv benchmark, two pandas versions

One benchmark from pandas' own [asv](https://asv.readthedocs.io) suite, run unchanged by
`bx bench PATH --adapter asv` under two pandas versions, each in its own virtual environment,
alternated round by round, then compared. The asv adapter reads asv's benchmark format as a
language (discovery, `params`, `setup`, `setup_cache`, asv's benchmark version hash) and times it
with pyperf: neither asv nor asv_runner is installed.

The suite is pandas' `asv_bench/` at one tag (`v3.1.1`), the same for both sides; only the
installed pandas differs. That is
[UC-04](https://github.com/benchx-dev/benchx/blob/main/docs/use-cases/UC-04-cross-env.md),
"fixed branch, comparing environments", driven the same way as `examples/numpy-venvs`.

The case is `strings.Methods.time_lower('str')`: one parameter combination of one benchmark.
`dtype="str"` is object dtype in pandas 2.x and the pyarrow-backed string dtype from pandas 3.0,
so the two sides are expected to differ.

## Run it

You need [`uv`](https://docs.astral.sh/uv/), git, and network access the first time. Then run the
steps below from this directory. A run takes under a minute after the venvs are cached.

Everything the steps create is in `pandas/`, `.venv-a`, `.venv-b` and `out/`, all git-ignored.
Start by removing what an earlier run left:

```sh
$ rm -rf pandas .venv-a .venv-b out
```

benchx itself runs only inside the two venvs, and step 2 installs it there, from this checkout
(`../..`). Outside a checkout, install `benchx` from PyPI there instead.

## What it does

### 1. Fetch the suite

A shallow, sparse clone of pandas at `v3.1.1`, with only `asv_bench/`
checked out (a few MB). Nothing is built: the benchmarks import the pandas installed in each
venv, and each case runs from a scratch directory, so the unbuilt checkout cannot shadow it.

```sh
$ git -c advice.detachedHead=false clone -q --depth 1 --branch v3.1.1 --filter=blob:none --sparse https://github.com/pandas-dev/pandas.git pandas
$ git -C pandas sparse-checkout set asv_bench
$ git -C pandas rev-parse --short HEAD
```

### 2. Prepare two venvs

`.venv-a` with pandas 2.3.3 and `.venv-b` with pandas 3.1.1, each
with pyarrow, pyperf and benchx. benchx builds nothing: the user prepares both environments.

```sh
$ uv venv -q --python 3.12 .venv-a
$ uv pip install -q --python .venv-a/bin/python pandas==2.3.3 pyarrow pyperf -e ../..
$ uv venv -q --python 3.12 .venv-b
$ uv pip install -q --python .venv-b/bin/python pandas==3.1.1 pyarrow pyperf -e ../..
```

### 3. Run the caller's loop

For each of the 4 rounds (the fewest that give a verdict), one call
per side, in its venv, with one slot per call:

```sh
slot=0
for round in 0 1 2 3; do
  for side in a:2.3.3 b:3.1.1; do
    . ".venv-${side%:*}/bin/activate"
    bx bench pandas/asv_bench --adapter asv "strings.Methods.time_lower('str')" \
        --run-key pandas-asv --label "env=pandas-${side#*:}" \
        --round "$round" --slot "$slot" --out out/pandas-asv
    slot=$((slot + 1))
  done
done
```

`pandas/asv_bench` holds `asv.conf.json`, whose `benchmark_dir` is the suite; its checkout
is the target. The exact case name selects one parameter combination; `--filter REGEX`
(asv's `--bench`) would select by pattern instead. Modules the venv cannot import (no
matplotlib, scipy, ...) are listed on stderr and skipped. Each call prints a table, one for
each of the 8 calls:

```text
case                                      median    mean ± sd            min .. max            n   status
----------------------------------------  --------  -------------------  --------------------  --  -----------------------------------
strings.Methods.time_lower [dtype='str']  890.2 us  937.6 us ± 122.9 us  835.2 us .. 1.202 ms  10  success  [setup-per-call-unbatched]
```

### 4. Compare

The sides are told apart by the `env` label, and the two `grep` lines fail the
example if the comparison was refused, or if it does not name `deps.pandas` as the difference:

```sh
$ bx compare --run pandas-asv --profile environments --label env --baseline pandas-2.3.3 | tee out/compare.txt
$ grep -q 'differs:.*deps\.pandas' out/compare.txt
$ ! grep -q 'failed invariants' out/compare.txt
```

```text
run pandas-asv  profile environments  baseline pandas-2.3.3  contender pandas-3.1.1
  differs: coordinates.environment.system.deps.pandas, coordinates.environment.system.deps.sha256
  caveats: baseline: setup-per-call-unbatched, contender: setup-per-call-unbatched

IMPROVED (1)
  strings.Methods.time_lower       wall-time  effect  -85.40%  noise   1.76%  rounds=4
```

(The numbers vary from run to run.) `effect` is the contender against the baseline, averaged
over the rounds' paired medians: `-85.40%` means pandas 3.1.1 takes about 15% of the time
pandas 2.3.3 does, so about 7x faster, well beyond the round-to-round `noise`. `differs`
names the environment facts that tell the sides apart: `deps.pandas` is the version the
collector printed, and `deps.sha256` is the hash of the collector's whole output, which
changes whenever any of its facts do. Here numpy and pyarrow are the same on both sides, so
pandas is the one difference.

### 5. List the series

One per environment, because the collected pandas version is part of
a series' identity. Every point carries the suite's revision (pandas `v3.1.1`), whichever
pandas it ran under: the target is the checkout the benchmarks come from, and what varies is
the environment, recorded by the collector.

```sh
$ bx series --quantity wall-time
```

## How it is configured

`benchx.conf.json` is found by walking up from the working directory, so `bx` reads it when run
from here:

- `adapter`: the harness (`asv`) and its precision, in pyperf's terms: 2 processes (asv's
  rounds) × 5 values (asv's repeat), 1 warmup, loops calibrated. Flags such as `--processes`,
  `--values`, `--min-time` and `--loops` override them per call. What neither gives comes from
  each benchmark's own `repeat`, `number`, `sample_time` and `warmup_time`.
- `collectors.deps`: `describe_env.py`, a **context collector**. benchx runs it once per call,
  in the environment the call runs in, and it prints the pandas, numpy and pyarrow it imports.
  That lands in each result's `environment.system.deps`, which is how the `environments`
  comparison knows what differs between the sides.
- `paths`: the store and the results go in `out/`, so `bx series` shows this run only.
  `$BENCHX_HOME` would relocate the store over this setting: do not set it.

Each result also records the installed project's version (`provenance.info.asv.installed`) and
asv's hash of the benchmark's code (`protocol.asv.benchmark_version`), so editing the benchmark
starts a new series.

## Caveats

- **`setup-per-call-unbatched`.** `Methods` has a `setup`, so, as in asv_runner >= 0.3.0, every
  timed call sees fresh state: one call per loop, setup re-run between calls outside the timer.
  For a ~100 µs call that timing includes some timer overhead, and the result is flagged. A
  benchmark whose calls leave the set-up state intact can declare `setup_scope = "sample"`.
- **Memory benchmarks** (`peakmem_*`, `mem_*`) and other non-`time_*` types are listed and
  skipped.
- **Unstable boot time.** pyperf's `boot_time` metadata is part of `environment.system`, which
  the `environments` profile uses to tell sides apart. On hosts where the kernel's boot time
  drifts (seen on WSL2), calls with the same label can then become separate sides, and the
  comparison fails the `two-sides` invariant. This is a benchx issue, not specific to asv.

## What is in this directory

| File | Role |
|---|---|
| `README.md` | this walk-through, which is also the script: it is what the example runs |
| `benchx.conf.json` | the project file: the adapter and its precision, the `deps` collector, the paths |
| `describe_env.py` | the collector: prints the pandas, numpy and pyarrow this Python imports |
