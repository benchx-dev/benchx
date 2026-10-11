# Design documents

Reading order for reviewers. The schema is the root; everything else is
designed around it (see the repository README for the philosophy).

| Document | What it defines | Status |
|---|---|---|
| [`benchmark-result-schema.md`](benchmark-result-schema.md) | What a result is: series identity, the open quantity vocabulary, the result and ingest contracts, comparison profiles, migration from existing tools | draft |
| [`system-decomposition.md`](system-decomposition.md) | The ten components, their roles and boundaries, traceability to user stories | draft |
| [`runner.md`](runner.md) | Executing benchmark work on one node: work orders, pipeline, failure handling. Runs prebuilt targets and never builds; enforces only on the process it launches | draft |
| [`runner-schema.md`](runner-schema.md) | JSON schemas for the runner's contract: work order, environment policy, environment identity, observed-context record. Aligned with `runner.md` and `benchmark-environments.md` | draft |
| [`harness-adapter.md`](harness-adapter.md) | Translating native harness output into results: the context document, mapping rules, driving half, context collectors, adapter catalog | draft |
| [`benchmark-environments.md`](benchmark-environments.md) | Who prepares the environment, what the runner controls on its launched process, and what benchx records, with recommendations per kind of environment | draft |
| [`comparator.md`](comparator.md) | Verdicts in history mode and run mode: the eligibility guard, dispersion-scaled verdicts, the comparison document | draft |
| [`prototype-scope.md`](prototype-scope.md) | What the minimal prototype demonstrates and the decisions locked for it | agreed |
| [`prototype-design.md`](prototype-design.md) | How the prototype is built: one CLI over a Python API, a Parquet store, three JSON documents | draft |

In review as pull requests: the work order (`work-order.md`) and the
Parquet form of a result (`measurement-result-parquet.md`, #30).

Not yet designed (roles fixed in the decomposition, documents pending):
workbench, scheduler internals, change detector, alerting & annotations,
views, series continuity and identity-policy authoring, the environment
plugin interface, importers (beyond schema §6).
