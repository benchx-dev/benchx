# benchx

A schema-first benchmarking workbench for running local A/B comparisons,
tracking performance over commit history, and publishing static benchmark
dashboards. Works with `pyperf`, `asv`, and Google Benchmark.

## Quickstart

Install from [PyPI](https://pypi.org/project/benchx/):

```console
pip install benchx                  # JSONL store, compare, validate, and bx view
# or: pip install 'benchx[parquet]' # adds PyArrow for Parquet stores and CI --runs
```

Run benchmarks locally, compare two checkouts, and inspect history in a browser:

```console
# Measure a Python benchmark module (or an ASV suite with: bx bench . --adapter asv)
bx bench benchmarks.py

# Paired A/B comparison across two checkouts
bx compare ../wt-base ../wt-head --profile revisions --suite benchmarks.py --rounds 5

# Build and serve the interactive static dashboard locally
bx view
```

## Where to go next

- **Use it locally:** [CLI & technical guide](docs/benchx.md) and
  [worked examples](docs/examples.md) (NumPy, pandas/ASV, Apache Arrow, pyperf,
  parametrized benchmarks).
- **See it live:** [Apache Arrow C++ dashboard](https://arrow.benchx.dev/),
  powered by [benchx-ci](https://github.com/benchx-dev/benchx-ci) and
  [benchx-arrow](https://github.com/benchx-dev/benchx-arrow).
- **Explore the architecture:** [Design overview](docs/design/README.md),
  [benchmark result schema](docs/design/benchmark-result-schema.md),
  [schema reference](https://benchx.dev/schemas/), and
  [user stories](docs/user_stories/README.md).

## Approach: schema first

BenchX is designed as a cluster of cooperating components (benchmark runners on
compute nodes, schedulers, harness adapters, result storage, static views, and
comparators) rather than one monolithic tool.

Everything flows out of one artifact: the **benchmark result schema**. It
defines what a measurement result is, what makes two results comparable, and
how histories of results are identified and preserved. Every component produces
or consumes the schema, rather than the schema being shaped by any single tool's
needs. The schema is grounded in metrology standards (VIM/GUM) and includes a
migration contract so history from existing tools is preserved.

## Grounded in user stories

To make sure the schema and components serve real needs, we started by
collecting [user stories](docs/user_stories/README.md) from Apache Arrow,
NumPy, SciPy, pandas, Narwhals, and OpenBLAS — who benchmarks, what they do
today, where it hurts, and what must not break.

## Live deployment and companion repositories

A live continuous-benchmarking deployment tracking Apache Arrow C++ benchmarks
on `main` is published at <https://arrow.benchx.dev/>.

In line with the three-layer split described in
[`docs/design/ci-workbench.md`](docs/design/ci-workbench.md#7-packaging-and-repository-boundaries),
cloud CI automation and per-project benchmark configuration live in companion
repositories so that `benchx` itself stays cloud-free and CI-vendor-neutral:

- [benchx-arrow](https://github.com/benchx-dev/benchx-arrow) — Apache Arrow C++
  benchmark tracking repository (project config, target provider, machine
  catalog, and GitHub Pages publishing for the
  [live dashboard](https://arrow.benchx.dev/)).
- [benchx-ci](https://github.com/benchx-dev/benchx-ci) — reusable GitHub Actions
  workflows and AWS EC2/S3 infrastructure templates for ephemeral continuous
  benchmarking.
- [benchx_examples](https://github.com/benchx-dev/benchx_examples) — local
  end-to-end examples (NumPy, pandas/ASV, Arrow, pyperf, Google Benchmark).

## Contents

- [`docs/benchx.md`](docs/benchx.md) — installation, CLI reference (`bx`), and
  implementation details.
- [`docs/examples.md`](docs/examples.md) — live deployments, CI setup, and local
  worked examples.
- [`docs/design/benchmark-result-schema.md`](docs/design/benchmark-result-schema.md)
  — the benchmark result schema and migration contract.
- [`docs/design/ci-workbench.md`](docs/design/ci-workbench.md) — unattended CI
  workbench lifecycle, staging, and repository boundaries.
- [`docs/design/measurement-result-parquet.md`](docs/design/measurement-result-parquet.md)
  — how a result is stored in Parquet (`src/benchx/parquet.py`).
- [`docs/user_stories/README.md`](docs/user_stories/README.md) — user story
  template and collected stories.
- `src/benchx/` — the implementation, with its tests in `tests/`.
- `src/benchx/schemas/` — JSON schemas for the benchmark result and other
  components (shipped with the package).

## Contributing & documentation site

We welcome contributions of all sorts: add a user story, try `benchx` on your
project, open an issue on the
[issue tracker](https://github.com/benchx-dev/benchx/issues/), or implement a
feature.

The documentation site (<https://benchx.dev/>) is built with
[Zensical](https://zensical.org). To preview it locally:

```console
uv run --with-requirements docs/requirements.txt python tools/gen_docs.py
uv run --with-requirements docs/requirements.txt zensical serve
```

`tools/gen_docs.py` generates `docs/index.md` and `docs/schemas/` (gitignored);
`zensical.toml` configures the site, and `.github/workflows/docs.yml` builds and
deploys it.

## Status

Beta status: the main features are available and ready for user testing; the
project is under active development, and we expect to start offering API
stability guarantees in the near future.

## License

Apache License 2.0.
