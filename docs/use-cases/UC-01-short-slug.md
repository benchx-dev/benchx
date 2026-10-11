# Benchmark Use Case Template

## UC-01: Run a single parametrized benchmark

| | |
|---|---|
| **Status** | draft |
| **Owner** | |
| **Last updated** | |
| **Related** |  |

### 1. Summary

A contributor runs a single parametrized benchmark in a fixed environment.
The benchmark parametrization is problem-dependent: when developing an algorithm
which involves linear algebra, they verify scaling of the run time versus matrix size;
when developing an array-agnostic variant of a routine, they compare performance
for varying array providers.


### 2. Motivation

In this use case, there is no comparison across history, build or runtime environments
or machines: the benchmark contains itself all necessary information via its parametrization.

There can be one or multiple parameters (problem size, execution mode, array provider);
the benchmark may report one or multiple measurements (CPU time, peak memory).


### 3. Actors and trigger

- **Actor:** contributor on a laptop 
- **Trigger:** a manually typed command
- **Frequency:** on demand


### 4. Workflow sketch

Currently, to run a single benchmark in SciPy, one does
(the workflow is similar for other projects which rely on `asv`)

```
$ spin bench -t time_eigvals

# snip

[50.00%] ··· Running (linalg.Bench.time_eigvals--).
[100.00%] ··· linalg.Bench.time_eigvals                                               ok
[100.00%] ··· ====== ============ =========== ============
              --                           module
              ------------------- ------------------------
               size   contiguous     numpy       scipy
              ====== ============ =========== ============
                20      contig      127±40μs    124±30μs
                20      nocont      121±30μs    128±30μs
                20     fcontig      108±30μs    106±30μs
               100      contig      9.14±3ms    15.0±8ms
               100      nocont     12.6±10ms    10.4±5ms
               100     fcontig      16.0±6ms    9.76±3ms
               500      contig        n/a      492±300ms
               500      nocont        n/a      572±300ms
               500     fcontig        n/a      676±300ms
               1000     contig        n/a      2.57±0.7s
               1000     nocont        n/a      3.48±0.5s
               1000    fcontig        n/a      3.75±0.06s
              ====== ============ =========== ============
```

Here `time_eigvals` resolves to the benchmark function `benchmarks/benchmarks/linalg.py::Bench.time_eigvals`, which is parametrized with three parameters,
`size`, `contiguous` and `module`.
The `spin bench` command runs in an existing environment and may set environment
variables before invoking the benchmarking too that its `bench` command abstracts.
In this design the target is the project's own in-place build (for `spin`, the one
`spin build` made), which benchx runs and records without building; a dirty tree
is allowed and recorded as dirty.

The default view is just display. However, results need saving according to the schema,
for a user might want to add their own view/analysis postprocessing, which is
too problem-specific to specify here.


### 5. Study design

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | free | |
| Code version | free | can be a dirty tree with local-only changes |
| Environment | free | runs in the current environment |
| Execution context | controlled | |

This is the simplest use case with a minimum set of requirements: nearly everything
can be free for this local-only, ad-hoc task.
The execution context is the only thing which is marked as controlled: we keep the same instrument, interleaving strategy etc the same for the all parameters.

### 6. Measurements

Not specified, may vary per scenario.


### 7. Comparison semantics

n/a


### 8. Schema implications

- Profile: new
- Fields required beyond the core: derived from the benchmark parametrization;
  A benchmark might be parametrized by multiple parameters. See open question 2 though.
- Comparability key: n/a
- Validation invariants: n/a
- Conflicts with existing use cases: n/a


### 9. Storage and lifecycle

- **Volume:** records per invocation
- **Retention:** session-scoped
- **Location:** local working dir
- **Does this data ever need to join against data from another use case?** No


### 10. Degenerate and failure modes

- What happens when a *controlled* coordinate silently isn't constant: Emit a warning if
the execution context drifts during the run.
- What happens with missing or partial data: Nothing drastic, benchmarking runs are
  manually triggered and are ephemeral. 
- What is the worst wrong conclusion someone could draw, and does the schema make it
  harder or easier to draw? n/a


### 11. Non-goals

- build/runtime environment control
- repository state (can be dirty)
- comparisons across commits, environments or machines


### 12. Open questions

1. Where is the information about the benchmarking strategy is stored: e.g. which
measurements to take (CPU time, GPU time, peak memory), how to take measurements
(how to synchronize the device) etc: in the benchmark itself, in a per-session config,
in a per-project config?

2. For a parametrized benchmark, when is parametrization resolved? In other words,
if a benchmark `time_foo` is parametrized with `(size, module)` for `size` being `[10, 100]` and module being `[numpy, cupy]`, for the schema, is it a single benchmark with four parameter variants, or is it a collection of four individual entries, `time_foo[10-numpy]`, `time_foo[10-cupy]`, `time_foo[100-numpy]` and `time_foo[100-cupy]`.

