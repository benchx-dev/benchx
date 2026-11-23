# benchx

A design for a continuous benchmarking system — not one monolithic tool, but a
cluster of cooperating components (benchmark runners on compute nodes,
schedulers, ingestion adapters, result storage, views, comparison and alerting
services) that together let a project measure its performance on every change
and trust what it sees.

## Approach: schema first

Everything here flows out of one artifact: the **benchmark result schema**. It
defines what a measurement result is, what makes two results comparable, and
how histories of results are identified and preserved. Every other component —
runner, scheduler, dashboard, alerter, migration tool — is designed *around*
the schema, producing or consuming it, rather than the schema being shaped by
any single tool's needs.

For this to work, the schema must be designed correctly up front: rigorous
about measurement semantics (units, estimators, uncertainty), neutral toward
harnesses and languages, and rich enough that future use cases can sit on top
of it without schema changes. The current draft is grounded in a review of
metrology standards (VIM/GUM) and of existing benchmarking systems and
harnesses, with a migration contract so that history from today's tools is not
lost.

## Grounded in user stories

To make sure the schema and the components serve real needs, started by collecting
[user stories](https://benchx-dev.github.io/benchx/user_stories/) from multiple projects
-- who benchmarks, what they do today, where it hurts, what must not break.
We surveyed Apache Arrow's
continuous benchmarking setup, NumPy, SciPy, Pandas, and several related projects from
the data science/numerical computing ecosystem.
The stories drive which components exist and what each must do; the schema must
allow all of them.

## BenchX usage and development

Install with `pip install benchx` (or `pip install 'benchx[parquet]'` for the local Parquet store) ([PyPI](https://pypi.org/project/benchx/)).

We welcome contributions of all sorts: add a user story, tell us about your use case, try
`benchx` on your workflow/project, open an issue if you found a rough edge,
implement a missing feature.

- Documentation: <https://benchx-dev.github.io/benchx/>
- Live deployment (Apache Arrow C++): <https://arrow.benchx.dev/>
- Issue tracker: <https://github.com/benchx-dev/benchx/issues/>
- Worked examples: <https://github.com/benchx-dev/benchx_examples>

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
  end-to-end examples (NumPy, Arrow, pyperf, Google Benchmark, ASV).

## Contents

- [`docs/benchx.md`](docs/benchx.md) — installation, CLI reference (`bx`), and
  implementation details.
- [`docs/examples.md`](docs/examples.md) — live deployments, CI setup, and local
  worked examples.
- [`docs/design/benchmark-result-schema.md`](docs/design/benchmark-result-schema.md)
  — the benchmark result schema and migration contract (the core design
  document).
- [`docs/design/ci-workbench.md`](docs/design/ci-workbench.md) — unattended CI
  workbench lifecycle, staging, and repository boundaries.
- [`docs/design/measurement-result-parquet.md`](docs/design/measurement-result-parquet.md)
  — how a result is stored in Parquet (`src/benchx/parquet.py`), with a small
  converter in `tools/json_to_parquet.py`.
- [`docs/user_stories/README.md`](docs/user_stories/README.md) — user story
  template and collected stories.
- `src/benchx/` — the implementation, with its tests in `tests/`.
- `src/benchx/schemas/` — JSON schemas for the benchmark result and other
  components (shipped with the package).

## Documentation site

The design documents, use cases, user stories, and a reference page per JSON
schema are published as a site built with [Zensical](https://zensical.org).
To preview it locally:

```
uv run --with-requirements docs/requirements.txt python tools/gen_docs.py
uv run --with-requirements docs/requirements.txt zensical serve
```

`tools/gen_docs.py` generates the landing page and the
schema reference pages into `docs/` (those paths are gitignored); `zensical.toml`
configures the site, and `.github/workflows/docs.yml` builds and deploys it.

## Status

Beta status: the main features are available and ready for user testing;
the project is under active development, and we expect to be able to
start offering API stability guarantees in the near future.

## License

Apache License 2.0.
