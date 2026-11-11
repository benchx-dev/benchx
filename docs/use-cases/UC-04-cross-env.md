## UC-04: Fixed branch, comparing environments

| | |
|---|---|
| **Status** | draft |
| **Owner** | MarcoGorelli |
| **Last updated** | 2026-09-10 |
| **Related** | [UC-01](UC-01-short-slug.md) (each single-environment run reuses the UC-01 workflow) |

### 1. Summary

A project would like to explore the impact of two different environments on performance.

The environment may differ in terms of dependency versions or build systems. The code, hardware, and revisions, however, stay the same.

For example, check the impact of `numpy==2.0` vs `numpy==1.26.4` on pandas operations.

### 2. Motivation

For several projects, a large part of their performance is due to the performance of their dependencies. For example:

- [pandas](https://github.com/pandas-dev/pandas) builds on NumPy and on PyArrow. How do different versions of these libraries affect pandas' performance?
  Is NumPy 2.0 comparable to NumPy 1.26.4, or does it need special treatment to maintain performance?

- [Polars](https://github.com/pola-rs/polars/issues/20471) is exploring replacing its datetime library from Chrono to Jiff. What will the impact be on performance?

### 3. Actors and trigger

- **Actor:** contributor or maintainer.
- **Trigger:** manual command.
- **Frequency:** manual. This is likely not a routine benchmark, but rather one to run when evaluating different build systems or dependencies.

### 4. Workflow sketch

The code doesn't vary here, only the environment does. The goal is to compare environments which
can't co-exist in the same interpreter (e.g. you can't import both `numpy==1.26.4` and `numpy==2.0.0` in the same
process), so this workflow can't operate under a single `benchx` invocation. Instead, we need to:

- Manually create two different environment which we wish to compare.
- Within each environment, reuse UC-01's command to run a benchmark, but add labels for ease of comparison later.
- Compare.

For example:
```console
$ python -m venv .venv-baseline && .venv-baseline/bin/pip install numpy==1.26.4
$ python -m venv .venv-variant  && .venv-variant/bin/pip install numpy==2.0

$ SESSION=$(benchx new-session)
$ for round in 1 2 3 4 5; do
    .venv-baseline/bin/benchx run run_benchmark.py --env-label numpy-1.26.4 --session-id "$SESSION" --round "$round"
    .venv-variant/bin/benchx run run_benchmark.py  --env-label numpy-2.0    --session-id "$SESSION" --round "$round"
  done

$ benchx compare-environments --session-id "$SESSION" --baseline numpy-1.26.4 --variant numpy-2.0

benchmark_name   env             time     vs. baseline
truncate         numpy-1.26.4    120 ns   -
truncate         numpy-2.0       131 ns   +9.2%
```

It's then up to the maintainers to determine whether the difference is acceptable.

### 5. Study design

The core of the use case. Every record is a measurement plus coordinates:

- **Code identity** - benchmark ID, parameters
- **Code version** - commit, patch/variant label, build configuration
- **Environment** - machine, OS, CPU, BLAS vendor and threading, tuning state
- **Execution context** - session, round, interleave position, instrument

State for each:

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | controlled | same benchmark ID, same params, in both runs |
| Code version | controlled | same commit in both runs |
| Environment | varying | this is the axis under test - dependency versions or build system differ |
| Execution context | controlled | interleave position alternates between environments |

### 6. Measurements

| Measure | Instrument | Unit | Direction |
|---|---|---|---|
| wall time | pyperf | ns | lower is better |
| peak memory (optional) | memray | bytes | lower is better |

- **Raw samples retained?** yes.
- **Is the instrument available in every environment this use case spans?**
  It should be, yes.

### 7. Comparison semantics

- **What comparison is valid?** within a session (the interleaved sequence of
  baseline and variant invocations, joined by shared `session-id`), the [minimum
  timing per environment is compared](https://docs.python.org/3/library/timeit.html#timeit.Timer.repeat).
- **Estimator:** minimum is usually recommended
- **What counts as a real difference?** Up to the user to determine.
- **Expected noise floor** this depends on the code being benchmarked, user should adjust interpretation accordingly.

### 8. Schema implications

- **Profile:** new. cross-environment-comparison
- **Fields required beyond the core:**
  - `env_label`: obligatory, human-readable tag for the environment a run was executed in.
  - `interleave_position`: obligatory (can't be null)
- **Fields required to be *absent* or explicitly null:** none
- **New fields not currently in the schema:**
  - `env_label`: string. Set by the caller at `run` time (`--env-label`).
  - `interleave_position`: integer. Set by the caller at `run` time (`--round`).
- **Comparability key:** same `session_id`, same `benchmark_name`, same commit, `env_label` differing between the two groups being compared.
- **Validation invariants:**:
  - For any `session_id` and `benchmark_name`, the two `env_label` groups pointed to by `--baseline`/`--variant` should have equal round counts.
  - Both groups share the same commit; `env_label` is expected to differ (that's the axis under test).
  - `interleave_position` must alternate between the two `env_label` groups.
- **Conflicts with existing use cases:** none.

### 9. Storage and lifecycle

Store session_ids, env_labels, interleave_positions, and timings.

### 10. Degenerate and failure modes

An environment may be missing or unusable, or a test may fail to run. Building each environment is the caller's job and its failure is outside benchx; the runner refuses a target it cannot identify and reports the reason (`runner-schema.md` §6).

### 11. Non-goals

- Cross-branch comparisons (code identity/version varying at the same time as environment).
- Comparing more than two environments in a single `compare-environments` call.
- Tracking environment comparisons over time/history across many revisions.
- Cross-arch comparisons.

### 12. Open questions
