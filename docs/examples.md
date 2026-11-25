# Examples

Live continuous-benchmarking deployments and local worked examples built on
`benchx`.

## Live deployments

| Project | Live dashboard | Tracking repository | Harness / scope | Infrastructure |
|---|---|---|---|---|
| **Apache Arrow C++** | <https://arrow.benchx.dev/> | [`benchx-dev/benchx-arrow`](https://github.com/benchx-dev/benchx-arrow) | Google Benchmark (Arrow C++ suites on `main`) | GitHub Actions + ephemeral AWS EC2 / S3 via [`benchx-dev/benchx-ci`](https://github.com/benchx-dev/benchx-ci), published to GitHub Pages |

If your project publishes a `benchx` deployment and would like it listed here,
open a pull request adding a row to this table.

## Continuous benchmarking setup

Continuous deployments follow the three-layer split in
[CI workbench §7](design/ci-workbench.md#7-packaging-and-repository-boundaries)
so that core `benchx` remains cloud-free and CI-vendor-neutral:

1. **Core package** ([`benchx-dev/benchx`](https://github.com/benchx-dev/benchx))
   — schemas, runner, adapters, local workbench (`bx`), store, and comparator.
2. **Reusable CI workbench** ([`benchx-dev/benchx-ci`](https://github.com/benchx-dev/benchx-ci))
   — reusable GitHub Actions workflows (`workflow_call`), AWS CloudFormation
   infrastructure template, wall-clock unit-budget loop, and staging compaction
   scripts parameterized by project configuration.
3. **Project tracking repository** (for example,
   [`benchx-dev/benchx-arrow`](https://github.com/benchx-dev/benchx-arrow)) —
   project-specific files only: `benchx.conf.json`, the target provider,
   machine catalog, and thin caller workflows. Use `benchx-arrow` as a minimal
   template when setting up continuous benchmarking for another project.

## Executed examples

These examples live in [`examples/`](https://github.com/benchx-dev/benchx/tree/main/examples)
in this repository. CI runs the code blocks of each README on every pull request, against the
benchx of that pull request, and the page shows the output of that run.

- [`pyperf-suite`](examples/pyperf-suite.md) — timing a suite of Python functions with
  `bx bench`, the local store, and `bx series` / `bx history`.

## Local worked examples

Standalone, runnable local examples live in
[`benchx-dev/benchx_examples`](https://github.com/benchx-dev/benchx_examples)
and are exercised by CI:

- `examples/pyperf-suite` — timing a Python function suite with `pyperf`
  (`bx bench` and replaying orders with `bx run`).
- `examples/numpy-venvs` — cross-environment comparison across two virtual
  environments ([UC-04](use-cases/UC-04-cross-env.md)).
- `examples/pandas-asv` -- cross-environment comparison run of a specific benchmark
  from the pandas ASV suite. Also shows how to run existing ASV benchmarks.
- `examples/arrow/demo/run.sh` — CMake + Google Benchmark walkthrough covering
  comparison profiles on a small C++ suite.
- `examples/arrow/archery-local` — target provider walkthrough
  (`benchx.conf.json` and `@CONFIG` targets).

See [benchx](benchx.md) for installation and the full `bx` CLI reference.
