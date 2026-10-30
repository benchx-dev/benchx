# Example: one benchmark, several parameter combinations

`bench_matmul.py` times `a @ b` for square float64 arrays, for N = 10, 50, 100 and
for numpy and torch: one function, six cases. `bx bench` runs each combination as its own
case, under [pyperf](https://pyperf.readthedocs.io).

## Run it

From `prototype/`, in an environment of your choice:

```sh
pip install -e . pyperf numpy torch
cd examples/matmul-params
export BENCHX_HOME=$PWD/out/home      # optional: keep the result store here, not in ~
bx bench bench_matmul.py --out out
```

This uses pyperf's defaults (20 worker processes x 3 values, 0.1 s calibration) and takes
several minutes, mostly importing torch in every worker.

## A quick version

```sh
bx bench bench_matmul.py --processes 2 --values 2 --min-time 0.01 --out out
```

About 15 seconds. The timings are rougher: each case has 4 values.

## Output

One row per combination, named by the function and its arguments:

```
case                              median    mean ± sd            ...
bench_matmul [n=10, xp='numpy']   1.627 us  ...
bench_matmul [n=10, xp='torch']   2.387 us  ...
...
bench_matmul [n=100, xp='torch']  23.88 us  ...
```

The result files are in `out/`, and `out/home/store.parquet` is the store. `bx` reports
twice as many results as rows: each case also records `load-average`.

## Choosing cases

```sh
bx bench bench_matmul.py bench_matmul                    # a function's name: all six
bx bench bench_matmul.py "bench_matmul(100, 'torch')"    # one combination
bx bench bench_matmul.py --filter "'torch'"              # a regex: the three torch cases
```

A regex is searched in `bench_matmul(10, 'torch')`, the function's name and the argument
values (not their names). `--filter 'bench_matmul$'` is therefore refused, since `$` leaves
no room for the arguments; the refusal says so.

## How the parameters are declared

```python
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
allocated inside the timed call.
