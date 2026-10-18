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

To make sure the schema and the components serve real needs, we are collecting
user stories from multiple projects — who benchmarks,
what they do today, where it hurts, what must not break. We surveyed Apache Arrow's
continuous benchmarking setup, NumPy, SciPy, Pandas, and several related projects from
the data science/numerical computing ecosystem.
The stories drive which components exist and what each must do; the schema must
allow all of them.

## Contents

- `docs/design/benchmark-result-schema.md` — the benchmark result schema and
  migration contract (the core design document).
- `docs/user_stories/` — user story template and collected stories.
- `schemas` -- JSON schemas for the benchmark result and other components.
- `prototype/` -- an in-progress implementation.
- `prototype/examples/` -- worked examples of benchmarking setup(s).

## Status

Pre-alpha. The main goal at this stage is to get the schema and the component
boundaries right. The project is under active development, and we expect to be able to
start offering API stability guarantees in the near future.

## License

Apache License 2.0.
