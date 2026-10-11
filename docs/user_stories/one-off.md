# One-off benchmarking user stories


# One-off benchmarking user stories (multiple projects)

**Project:** multiple projects, individual stories have links to examples 
**Current benchmarking setup:** One-off, ad hoc benchmarking to guide ongoing development.
**Contact:** Evgeni

## Who uses benchmarks here

- **Contributors** -- want to know whether a change under development affects performance
  under some measure (raw speed, memory, scaling)
- **Reviewers** -- same questions, only during the review stage

## Stories

### Performance optimize a function

> As a contributor, I change an algorithm for a single function, and I want to see if
> my current version of a function improves on an existing version.
> Benchmarking results are transient, and need not be saved for posterity, as I am
> iterating over implementations.
> I operate in an implement-test-benchmark cycle, where on each iteration I want
> to both check if my current iteration is still correct (testing) and improves
> performance (benchmarking).

**Today:** Write an ad-hoc benchmarking script, using `%timeit` or similar.
As an illustrative example, see https://github.com/numpy/numpy/pull/32165
**Pain:** One-off benchmark scripts typically differ from the performance tracking for
the project, and need to be converted to the tracking framework format (e.g. going from
`%timeit`-based benchmarks to the `asv` format) 


### Check multiple performance targets

> As a contributor, I still work on a single function, and I want to be able to check
> that my performance optimization has an acceptable peak memory characteristics.

**Today:** Write two ad-hoc benchmarks, one with `%timeit`, the other with `%memit`
**Pain:** Have to convert the ad-hoc benchmarks the framework format; `asv` does
only records a single "benchmark result", thus need to write two separate benchmarks;
analyzing results also is separate: there is no viewer to check both memory and speed
in a single view or a table.


### Check scaling

> As a contributor, I am replacing an algorithm with a different big-Oh cost, and I
> want to be able to find a crossover point, where the asymptotic performance beats
> the fixed cost.

**Today:** Write an ad-hoc benchmark with an ad-hoc looping over a parameter, run
the benchmarks for two versions, manually assemble results for analysis.
For the analysis, I might want to check log-log or log-linear scaling.
**Pain:** No support for this mode in either a benchmark runner or viewer. Users have
to use ad-hoc scipts which need to reach into results stored in an under-documented
storage format. Here is one example:
https://github.com/OpenMathLib/BLAS-Benchmarks/blob/main/analyze.results.example.ipynb,
which has to make assumptions about the name of the json file with results from an
`asv` run.


### Compare a benchmark across build modes

> As a maintainer, I want to know if building an extension with `-ffast-math` brings
> meaningful performance for a function from this extension.
> I also want to compare performance of a SIMD version of the same function. And
> I need to make sure that all of these versions still give the correct result.

**Today:** With `asv`, I probably need three separate benchmarking projects, one for
each build variant; with an ad hoc harness, I need to manually build three times,
rerun benchmarks, and then manage results manually.
**Pain:** Code duplication, manual and error-prone management of build environments,
manual and error-prone management of benchmark results.



## Wishes

- Should be easy to reuse the in-development benchmarks for performance tracking;
- Track multiple "benchmarking targets" from the same benchmark (memory and speed);
- Benchmark parametrization and simple-to-add custom views (two panels: log-log
  performance vs problem size on one panel + memory on the other panel);
- Being able to compare uncommitted changes with git HEAD;
- Simple in-place rebuilds in the current environment.
- A clear and simple way to specify either build instructions or a predefined build
  environment.

> **Design note:** benchx does not build. Build instructions stay with the
> caller's own tools, and benchx is handed the resulting builds and records them
> (`docs/design/benchmark-environments.md` §2.3). The wish is a non-goal for benchx itself.

## Scale and constraints

Typically these are relatively microbenchmarks to be run locally.
