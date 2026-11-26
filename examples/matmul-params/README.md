# Example: one benchmark, several parameter combinations

In this demo, we show how to run a parametrized benchmark: time a matrix multiply, `a @ b`,
for square arrays of several sizes, represented as numpy arrays and torch tensors.

## Install the required dependencies


```sh noexec
python -m venv .venv && . .venv/bin/activate
pip install benchx[parquet]
pip install pyperf numpy
pip install torch --index-url https://download.pytorch.org/whl/cpu    # the CPU build
```

This uses pyperf's defaults (20 worker processes x 3 values, 0.1 s calibration) and takes several minutes, mostly importing torch in every worker.

## Run a quick version

By default, `benchx` uses `pyperf`'s defauls: 20 worker processes x 3 values each, and 0.1s calibration.
In this demo, we use a quick-and-dirty version, so that the run time is smaller but the results will be noisy

```sh
$ export QUICK="--processes 2 --values 2 --min-time 0.01"
$ bx bench bench_matmul.py $QUICK
```

The output shows a row per combination, named by the function and its arguments:

```text
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='numpy']   1.948 us  1.985 us ± 259.5 ns  1.731 us .. 2.313 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=10, xp='torch']   2.125 us  2.124 us ± 18.13 ns  2.103 us .. 2.141 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='numpy']   9.179 us  9.18 us ± 1.501 us   7.875 us .. 10.49 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='torch']   18.07 us  18.1 us ± 388.3 ns   17.75 us .. 18.5 us   4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='numpy']  54.69 us  55.53 us ± 3.073 us  52.81 us .. 59.94 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='torch']  25.45 us  25.48 us ± 278.8 ns  25.2 us .. 25.83 us   4  success  [dirty-subject-tree, dirty-benchmark-tree]

12 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/bench-20261126T154006Z  (order sha256:ba913ce6b140)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 12, duplicate 0

```

As usual, bare result files are in `out/`, and `out/home/store.parquet` is the store. `bx` reports
twice as many results as rows: each case also records `load-average`.

## Choosing cases

We can select specific cases by either giving exact parameter values or a regex. Note that since we are parametrizing
the benchmark with string arguments, we have to use the quoted strings in CLI.

```
$ bx bench bench_matmul.py bench_matmul  $QUICK                  # a function's name: all six
$ bx bench bench_matmul.py "bench_matmul(100, 'torch')"  $QUICK  # one combination
$ bx bench bench_matmul.py --filter "'torch'" $QUICK        # a regex: the three torch cases
```

A regex is searched in `bench_matmul(10, 'torch')`, the function's name and the argument
values (not their names). `--filter 'bench_matmul$'` is therefore refused, since `$` leaves
no room for the arguments; the refusal says so.

# Comparing numpy and torch

Let's compare performance of the matrix multiply between NumPy and PyTorch for the same array sizes.

The two libraries are the two values of `xp`, so `bench_matmul` is also a
[UC-02](https://github.com/benchx-dev/benchx/blob/main/docs/use-cases/UC-02-variants.md) benchmark: the same function, run once with each
library, with the rounds interleaved. `n` is the other parameter, so each size is its own comparison and
`--vary xp` says which parameter is the one that differs.

Note that `bx compare` requires a specific order of runs, with alternating comparison sides, to minimize
the noise.
There are two ways we can run the comparison. Consider them in turn.

## A manual loop of `bx bench` calls

We run a manual loop of `bx bench` calls and invoke `bx compare` in the so-called _run mode_ (i.e.,
it pulls precomputed data for comparisons from the store).

Each call runs one library, with `--filter`, as one side of a session; 
`--round` and `--slot` place it, and `bx compare --run` compares the sides:

```sh
for r in 0 1 2; do
  bx bench bench_matmul.py --filter "'numpy'" --run-key mm --round $r --slot $((2*r))   $QUICK
  bx bench bench_matmul.py --filter "'torch'" --run-key mm --round $r --slot $((2*r+1)) $QUICK
done
```

The output is a little noisy because each `bx bench` call emits its output:


```text
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 0, slot 0)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='numpy']   1.942 us  1.942 us ± 126.7 ns  1.827 us .. 2.058 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='numpy']   10.5 us   10.51 us ± 10.11 ns  10.5 us .. 10.52 us   4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='numpy']  55.09 us  55.53 us ± 2.828 us  52.59 us .. 59.38 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:d1d63f2006a6)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 0, slot 1)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='torch']   2.086 us  2.09 us ± 22.13 ns   2.072 us .. 2.117 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='torch']   17.08 us  17.08 us ± 502 ns    16.62 us .. 17.53 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='torch']  22.56 us  22.63 us ± 393.7 ns  22.26 us .. 23.15 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:863bed6c59db)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 1, slot 2)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='numpy']   1.938 us  1.939 us ± 141.6 ns  1.813 us .. 2.066 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='numpy']   10.56 us  10.56 us ± 3.904 ns  10.56 us .. 10.57 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='numpy']  57.25 us  57.35 us ± 2.792 us  54.58 us .. 60.34 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:689139a1c409)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 1, slot 3)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='torch']   2.136 us  2.136 us ± 11.74 ns  2.124 us .. 2.146 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='torch']   17.3 us   17.29 us ± 268.6 ns  16.97 us .. 17.6 us   4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='torch']  23.94 us  23.88 us ± 378.9 ns  23.42 us .. 24.21 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:241f9080ac82)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 2, slot 4)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='numpy']   2.005 us  2.03 us ± 198.5 ns   1.822 us .. 2.289 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='numpy']   10.58 us  10.58 us ± 51.98 ns  10.53 us .. 10.63 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='numpy']  55.24 us  55.41 us ± 2.074 us  53.2 us .. 57.96 us   4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:0f09afb13b87)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
measuring bench_matmul.py on /home/ev-br/proj/quansight/benchx (round 2, slot 5)
precision: 2 processes x 2 values (config: /home/ev-br/proj/quansight/benchx/examples/matmul-params/benchx.conf.json)
case                              median    mean ± sd            min .. max            n  status
--------------------------------  --------  -------------------  --------------------  -  ---------------------------------------------------
bench_matmul [n=10, xp='torch']   2.1 us    2.103 us ± 8.111 ns  2.096 us .. 2.114 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=50, xp='torch']   17.89 us  17.95 us ± 229.8 ns  17.74 us .. 18.28 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]
bench_matmul [n=100, xp='torch']  22.94 us  22.94 us ± 760.3 ns  22.16 us .. 23.72 us  4  success  [dirty-subject-tree, dirty-benchmark-tree]

6 result(s) -> /home/ev-br/proj/quansight/benchx/examples/matmul-params/out/mm  (order sha256:431034895aa7)
/home/ev-br/proj/quansight/benchx/examples/matmul-params/out/home/store.parquet: ingested 6, duplicate 0
```

Finally, we run the comparison: note that we specify the run key explicitly; we compare across variants of the same
function via `--profile variants`; we vary the parameter `xp`: 


```sh
bx compare --run mm --profile variants --vary xp --baseline "'numpy'"
```

```text
run mm  profile variants  baseline 'numpy'  contender 'torch'  [local-only]
  caveats: baseline: dirty-benchmark-tree, baseline: dirty-subject-tree, contender: dirty-benchmark-tree, contender: dirty-subject-tree

REGRESSED (2)
  bench_matmul [n=10]              wall-time  effect   +7.45%  noise   1.59%  rounds=3
  bench_matmul [n=50]              wall-time  effect  +65.15%  noise   1.96%  rounds=3

IMPROVED (1)
  bench_matmul [n=100]             wall-time  effect  -58.56%  noise   0.25%  rounds=3
```

UPTOHERE

The result is one unit per size, with numpy as the baseline and torch as the contender: the effect is
torch's median time relative to numpy's, so `REGRESSED` means torch took longer, and the noise is taken
across the five rounds. The units are grouped by verdict. This is the output of the loop above with the
quick settings (`--processes 2 --values 2 --min-time 0.01`, added to both calls).

Here torch is slower than numpy at N=10 and N=50 and takes about half the time at N=100. The N=50 noise is high
with the quick settings, which take only a few values per case. `--baseline` is spelled as the
value is recorded, quotes included.

One `bx bench bench_matmul.py` call is not enough. It measures all six cases in one order, with no round and no
slot: a grid of timings, not an alternation, so `bx compare --profile variants` refuses it
(`alternating-slots`: "a result has no slot…") and the message names the two ways to get an alternating run,
the loop above and the one-command form below.

**In one command.** Measure mode runs the same loop, listing the cases once and writing one order per library
and round:

```sh
bx compare . --profile variants --adapter pyperf --suite bench_matmul.py --vary xp --rounds 5 --processes 2 --values 2 --min-time 0.01
```

It prints the same table, with the numbers of its own run (with the quick settings: +17.9%, +76.8% and
-52.7% for N=10, 50 and 100). Both forms have the quick version's `--processes 2 --values 2 --min-time 0.01` here
for a rougher, faster comparison; the loop above takes about a minute and a half with them, mostly importing
torch in every worker.

## How the parameters are declared

```python noexec
bench_matmul.params = [[10, 50, 100], ["numpy", "torch"]]   # one list per argument
bench_matmul.param_names = ["n", "xp"]
```

Combinations are the product of the lists, in this order, and the function is called with
one of them. A value is recorded as its `repr`, so the library is given by its name and
imported on first use, not passed as a module.

## What is timed

The whole body of `bench_matmul` is timed: the call to `operands(n, xp)` as well as `a @ b`.
`operands` is cached, so the arrays are made once per worker process, on its first call (a
calibration or warmup call), not in every call. What every call still pays is the cache
lookup, about 60 ns here: roughly 4% of the numpy time at N=10 and under 0.2% at N=100.
The timings include it, for numpy and torch alike. The result array of `a @ b` is also
allocated inside the timed call. In a comparison of the two libraries the lookup is a fixed cost
on both sides: it cancels in the difference but not in the percentage, so at N=10 the
percentage reads about 4% smaller than it is.
