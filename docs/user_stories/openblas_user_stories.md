# OpenBLAS user stories

**Project:** [OpenBLAS](https://github.com/OpenMathLib/OpenBLAS)
**Current benchmarking setup:** One set of benchmarks is on `codspeed`, runs in each PR;
The other set of equivalent `asv` benchmarks, runs via a cron job github action on
cloud machines provisioned via cirun.io.
**Contact:** Evgeni

## Who uses benchmarks here


**Contributor** and **Reviewer** check regressions on a PR.
**Maintainer** manually checks the cron job history to find regressions.


### Check/quantify a performance impact of a pull request

> A contributor wants to check that their pull request does not introduce regressions,
> or that it improves performance.

**Today:** [`codspeed` integration](https://app.codspeed.io/OpenMathLib/OpenBLAS) runs
on each pull request, and reports results back to a PR. Was reporting back, until
a token expired, that is.
**Pain:** There is essentially no useful signal: OpenBLAS performance improvements
come from low-level kernels specific to hardware architecture; `codspeed` does not
offer any control of the hardware.

### Actually check/quantify a performance impact of a pull request

> A contributor wants to check that their pull request does not introduce regressions,
> or that it improves performance.

**Today:** A contributor runs a bespoke benchmark on their local machine, copy-pastes
the results into the GitHub interface.
See, for example, https://github.com/OpenMathLib/OpenBLAS/pull/5986
**Pain:** The benchmark collection is just a collection of (mostly) C programs; there
is no automation, and running benchmarks is manual; results are not stored anywhere;
there is no provenance, no regression tracking. Just some numbers from somebody's
machine, copy-pasted free-form into a pull request comment.


### Track regressions over time

> A maintainer wants to monitor performance, find regressions and track them back to
> the origin.

**Today:** [An `asv` benchmark suite](https://github.com/OpenMathLib/BLAS-Benchmarks)
runs on a weekly cron schedule on cloud instances of several architectures (x86, arm)
via github actions, on self-hosted runners provisioned via `cirun`. Results are uploaded
to the standard `asv` static website view at https://www.openmathlib.org/BLAS-Benchmarks/.
Raw results are uploaded along with a view, and an example notebook for custom analysis
is provided in the benchmark source,
https://github.com/OpenMathLib/BLAS-Benchmarks/blob/main/analyze.results.example.ipynb
**Pain:** `asv` views are limited and inflexible, especially for jobs of varying
computational complexity; custom views are hard to incorporate into the `asv`-built
website; asking questions beyond the narrow `asv` remit (which is to track performance
over time only) requires manual work and is difficult to track over time.


## What must not break

- setting up web views does not require frontend work
- ability to deploy on a wide range of hardware
- ability to control compilation options, threading, versions of dependencies


## Wishes

- custom views for parametrized benchmarks ("track the threshold for matmul's `O(N**3)`
scaling")
- cross-benchmark comparison ("track perfomance of `dgemm` on a graviton3 vs x86_64,
over time, on the same plot")
- have a unified syntax for writing benchmarks (here, `codspeed` vs `asv`)


## Scale and constraints

- for a volunteer project with limited funding: weekly cron runs, a dozen of
  benchmarks only;
- scarce resources for maintenance, no dedicated perfomance engineer role.
- need access to various hardware types

