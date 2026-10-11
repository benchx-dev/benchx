## UC-03: Run a benchmark comparison across two revisions

| | |
|---|---|
| **Status** | draft |
| **Owner** | Maanas Arora |
| **Last updated** | |
| **Related** | |

### 1. Summary

A contributor wants to benchmark two revisions in a fixed environment and
compare the results to check if performance improved or there is a regression.
This can be run locally or in CI. The project's target provider (for NumPy,
`spin`) builds both revisions and hands benchx two prepared targets; benchx
does not check out or build revisions, so the setup has the same shape as
UC-04.

### 2. Motivation

Those working on performance-sensitive code very frequently run benchmarks
comparing two revisions, usually between a PR and the `main` branch.
For example, on NumPy, this can happen (generally) 1-5 times a week, depending
on the number of performance relevant PRs in flight.

Currently, this process is manual using `asv` and the benchmarks can be quite
slow and statistically unreliable. Regressions can therefore be easily missed,
and ad-hoc improvements are not usually added to the `asv` suite due to bloat.
For example, see https://github.com/numpy/numpy/pull/31345 and
https://github.com/numpy/numpy/pull/31431 where a benchmark had to be
repeatedly run manually, and the lack of reliability created confusion, lost
time, and the chance of shipping a regression.

### 3. Actors and trigger

- **Actor:** contributor or maintainer on a laptop or the GitHub interface
- **Trigger:** a command they type on a laptop, or approving a CI run
- **Frequency:** on demand or per specific commits as requested

### 4. Workflow sketch

**Path A - local**:

Using the `spin` library (which currently uses `asv` underneath).

An example snippet (adapted from https://github.com/numpy/numpy/pull/31345 for this new use case):

```
$ spin bench -t Sort.time_argsort --compare main

Comparing 4b3a4815 <reverse-sorts> against 532a1c5d <main>

| Change | kind  | dtype   | array_type             |  Before |   After | Ratio [95% CI]       | Pairs |
|--------|-------|---------|------------------------|---------|---------|----------------------|-------|
| slower | merge | int32   | ('sorted_block', 1000) | 8.26 ms | 9.08 ms | 1.099 [1.091, 1.107] |    60 |
| faster | quick | float32 | ('reversed',)          | 23.5 ms | 21.8 ms | 0.928 [0.901, 0.955] |   120 |
| faster | merge | uint32  | ('sorted_block', 100)  | 12.4 ms | 11.4 ms | 0.919 [0.910, 0.928] |    60 |
| faster | quick | int64   | ('sorted_block', 100)  | 39.0 ms | 35.8 ms | 0.918 [0.897, 0.939] |   120 |
| faster | quick | int64   | ('sorted_block', 10)   | 39.5 ms | 36.1 ms | 0.914 [0.899, 0.929] |   120 |
| faster | quick | float32 | ('uniform',)           | 1.55 ms | 1.37 ms | 0.884 [0.871, 0.897] |    60 |
| faster | merge | int32   | ('sorted_block', 10)   | 20.4 ms | 17.6 ms | 0.863 [0.858, 0.868] |    60 |

7 of 84 comparisons shown: 6 faster · 1 slower · 0 inconclusive.
77 unchanged, hidden.  --show-all to list them.
Equivalence band: ±2%.

```

This compares the currently checked-out branch with the local `main` branch on
a parametrized `Sort.time_argsort` benchmark with parameters (at that time)
`kind`, `dtype`, and `array_type`.

The `spin bench` command runs in an existing environment and may set environment
variables before the benchmark run, abstracted by the CLI interface.

In this design `spin` is the target provider and the front end: it builds the
current checkout and `main` (each cached by commit and configuration), then
calls benchx with the two prepared targets. benchx's session loop issues one
work order per side per round, alternating the sides under one `run_key`, runs
the comparator with the `revisions` profile, and returns the comparison
document for `spin` to render (`system-decomposition.md` §3.4). Environment
variables such as thread caps go in the work order's environment policy, not
in `spin`'s shell. Plain `spin bench` (UC-01) is the same path with one target
and no comparison.

---

**Path B - CI**: (Future - not present on NumPy yet)

Add a comment on the GitHub PR that can look like this:

```
/bench
```

A bot will produce a comment showing benchmark results and a summary.

### 5. Study design

| Coordinate | Role | Notes |
|---|---|---|
| Code identity | controlled | same benchmark ID and parameters |
| Code version | varying | the independent variable |
| Environment | controlled | same machine and configuration |
| Execution context | controlled | sessions are interleaved |

Only the code version varies for this benchmark: we are running across the same benchmark
and parametrization, and keeping the environment and execution context as controlled as
possible ensures reliability.

### 6. Measurements

| Measure | Instrument | Unit | Direction |
|---|---|---|---|
| wall time | pyperf | s | lower is better |

- **Raw samples retained?** yes - we can use the raw samples from pyperf with
`dump --raw`, see https://pyperf.readthedocs.io/en/latest/cli.html#pyperf-dump
- **Is the instrument available in every environment this use case spans?**
  The runner core is cross-platform. However system tuning is not fully supported
  outside of Linux: https://pyperf.readthedocs.io/en/latest/system.html# (only
  a note - we are likely to separate environment control from the runner)

### 7. Comparison semantics

- **What comparison is valid?** paired
- **Estimator:** runner-dependent and stored - median, mean, or minimum, etc.
- **What counts as a real difference?**: statistical tests, TBD, ensure the test is
  reproducible from the recorded statistics
- **Expected noise floor**: runner-dependent, but A/A testing may be useful

### 8. Schema implications

**The section this document exists for.**

- **Profile:** new, `comparison`
- **New fields not currently in the schema:**
  - **interleave position**: must be stored for each measurement.
    However, see open question 1.
  - **ratio, ci_low, ci_high, classification**
- **Comparability key:** records must *differ* in the code version (the revision)
  and *agree* on everything under code identity.
- **Validation invariants:** all records in a comparison share all of
  `project_id`, `name`, `parameters`, `environment`, `observed_context`.
  The `environment` is compared by its hash.

### 9. Storage and lifecycle

- **Volume:** 1 parametrized record per invocation
- **Retention:** session-scoped, but see open question 2
- **Location:** local working dir
- **Does this data ever need to join against data from another use case?** No

### 10. Degenerate and failure modes

- Missing data: if the process is stopped before all interleaved runs are complete,
  the series cannot claim the profile.
- Reject dirty trees - the comparison is invalid if there is no way to validate either
  revision. The schema should make `dirty` non-nullable in this profile: the
  `revisions` profile's `clean-tree` invariant fails a dirty or unknown side.
  The comparison's environment policy carries a `tree.clean` verify rule with
  `refuse`, so the run is refused before any measurement. This applies to comparisons only: a single-revision run (UC-01)
  may be dirty and is recorded as dirty.
- Build failure of either revision happens in the target provider, before any
  work order exists, and is reported to the caller; there is no result to
  compare.

### 11. Non-goals

- Comparisons across machines.
- CI orchestration (beyond provision of CLI commands).

### 12. Open questions

1. For storing interleaving positions / other data, do we create pair
   identities under the comparison context?
2. For the future CI path, do we need a shared store for validation?
3. Should `--compare` allow a dirty tree as a local-only comparison keyed on
   working-tree ids, so a contributor can compare uncommitted work on the
   current branch without committing? This use case rejects it (§10); the
   schema would permit it (`benchmark-result-schema.md` §5.5).
