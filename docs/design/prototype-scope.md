# Minimal Prototype Scope

**Status:** Agreed scope for a first prototype; decisions recorded 2026-08-30, revised 2026-09-25 to follow the merged design documents and 2026-09-29 for the runner-does-not-build stance
**Companion to:** `prototype-design.md` (how it is built), `benchmark-result-schema.md`, `benchmark-environments.md`, `harness-adapter.md`, `comparator.md`, `runner-schema.md` (the work order), and `system-decomposition.md` §3.4 (the workbench)

## 1. The slice

A thin end-to-end path proving every core contract once:

```
two prepared Google Benchmark build directories
  → `bx compare` (measure mode) alternates the two sides over R rounds, one work order each
  → runner + adapter → result documents (files)
  → file-drop ingest, by the run itself (the `bx run` call) → the local store: one Parquet file
  → comparator, run mode (profile: revisions or environments)
  → comparison document (JSON + readable table)
```

The user prepares both sides with their own tools; benchx records them and
builds nothing (`benchmark-environments.md` §2). The same slice runs in two
shapes:

- **Ad hoc:** no project, no configuration, no server. The orders omit
  `project` and the results are thin (schema §4.1). They still land in the
  local store, and `bx compare --results` can compare the result files
  through a throwaway store instead.
- **Tracked:** orders name a project, and the results in the local store join
  that project's series, where `bx series` and `bx history` query them.

Two comparisons are demonstrated, both as two build directories measured in
one run:

- **Revisions** (UC-03): two checkouts of one source at different commits,
  built the same way. The sides differ in subject tree id.
- **Environments** (UC-04 shape, Arrow local story): one checkout built twice
  with one build option changed, hardened against plain. The sides differ in
  subject configuration and are named by a label.

## 2. Component scope in the prototype

| Component | In prototype | Deferred | Out of scope |
|---|---|---|---|
| Runner | execute a `build` or `working_tree` target per a work order; apply the order's `environment_variables` to the launched process; capture the zero-configuration snapshot, the build configuration, and source identity; account for planned cases; refuse an order it cannot apply exactly, including one that names a deferred field | `python_env` targets, context collectors that run after the harness or per case (the prototype runs them once per order, before the cases), environment policy (enforcing launch settings, verifying requested hardware, refusing an unsatisfiable run) | building revisions, tuning the machine (`benchmark-environments.md` §2.3) |
| Adapter | Google Benchmark, both halves: drive filter and repetitions, translate output; pyperf, both halves, for Python functions in a working tree, through `bx bench` and `bx compare` (the default adapter of both) | all other harnesses; pyperf's `tags`, `date`, per-run load and frequency, and `pyperf check` | — |
| Store | one Parquet file; ingest per schema §5.4 (validated and durable, auto-create, unit-conflict rejection, idempotency, attempt integrity); one default identity policy; queries: list series, series history, get result | continuity events, quarantine, artifact storage, identity-policy authoring | — |
| Comparator | run mode with the `revisions` and `environments` profiles; eligibility guard; comparison document | history mode; `single-run`, `variants`, and `cross-machine` profiles; batch requests; provisional noise rules | — |
| Workbench session loop | the project file `benchx.conf.json` (one file, in the project root: provider, project, harness and suite, quantities, precision, environment variables, timeouts, paths, comparator policy; flags over file over defaults); `bx bench` (one order) and `bx compare` in measure mode: writes one work order per side and round, alternates them, delivers the results, then compares; takes each side as a build directory or as `@CONFIG`, a call to the project's target provider (`describe` or `prepare`), whose description may be a build or a working tree; either harness, with the same `-e` variables on both sides; a bare path is a build directory for Google Benchmark and a working tree for pyperf; `bx bench` is the same path for one order; `bx target` asks for a description directly; copies the description's declared text (`how_built`, `activation`, `shell`, `provider`) into the order | provider-built revisions (a source ref other than `WORKSPACE` is the provider's business), an `artifact` target, a declared `build` | workbench packaging, front ends such as `spin bench` |
| Documents | work order (`schemas/work-order/0.1.0`), result (schema §5.2), comparison document | — | — |

**Out entirely:** scheduler, change detector, alerting, views beyond query
output, importers, workbench packaging (the prototype's CLI *is* the
proto-workbench).

## 3. Success criteria

1. The same result document is valid as a local file and as an ingest
   payload, unmodified (promotion is a copy).
2. Re-ingesting a delivered directory is a no-op (idempotency observable).
3. A quantity arriving with a conflicting unit is rejected with the
   machine-readable taxonomy code.
4. A workload variant measured on only one side, or with unequal rounds, is
   reported as missing or as a failed profile invariant, never silently
   dropped.
5. A small real change on a quiet benchmark is flagged while a larger swing
   on a noisy one is not, demonstrated on real Google Benchmark output for
   both profiles.
6. Every result traces to its work order, and every verdict to the
   `(producer, ingest_key)` of the results it consumed.
7. The ad hoc shape works end to end with no project, configuration file, or
   store path. A single run on a dirty tree is accepted and recorded as dirty.
   A comparison with a dirty side, under `revisions` or `environments`, is
   labeled local-only and names the dirty side (UC-03 §10, schema §5.5); a
   `revisions` side with no working-tree id is refused.
8. The same comparison computed from a throwaway store and from a persistent
   store yields the same document.

## 4. Decisions locked for the prototype

- benchx builds nothing and tunes nothing; it records the environment and
  applies only launch settings (`benchmark-environments.md` §2). The prototype
  applies `environment_variables` and defers the rest of the environment
  policy.
- The work order is `schemas/work-order/0.1.0` (`runner-schema.md`), plus one
  optional integer, `slot` (`prototype-design.md` §2).
- One command, `bx compare`, has a read mode and a measure mode
  (`prototype-design.md` §1).
- The local store is a single Parquet file in one place,
  the project's `store.parquet` (`paths.store` in `benchx.conf.json`), and `bx run` delivers to it. JSON is for
  documents in transit (work orders, result files, comparison documents),
  never for the store.
- Ingest acknowledgment = validated and durable (schema §5.4).
- Entities auto-create; unit conflicts reject (schema §5.4).
- Batches are per-result, non-atomic (schema §5.4).
- The comparator runs in run mode only; history mode layers on later.
- Verdict = dispersion-scaled threshold, *k* configurable per invocation.
- The eligibility guard lives in the comparator.
- Comparator and change detector remain separate components.
