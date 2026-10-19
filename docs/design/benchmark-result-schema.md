# Benchmark Measurement Result Schema and Migration Contract

**Status:** Minimal draft incorporating source, metrology, and design-partner review

**Author:** Rok Mihevc

**Review date:** 2026-09-11

**Revision (2026-10-18, draft for review):** facts are placed by what kind of fact they are, not by the role they play in a comparison. `comparison_context` becomes `method`, `environment.metadata` becomes `environment.facts`, `observed_context` is removed, and conditions such as temperature and load become sibling measurement results. Roles move to projections (`schemas/projection/0.1.0`): a project's identity policy is the one the store maintains, and profiles and other views are projections over the same results. See §1, §4.2, §4.3, §5.5, and §9.

## 1. Purpose

This document defines the minimum model needed to ingest, compare, and migrate computing benchmark results without inventing statistical meaning. Detection, alerts, thresholds, scheduling, run lifecycle, and environment setup are out of scope.

> A **measurement result** records the evidence from one measurement attempt for one quantity and one workload variant, together with its conditions and provenance. It holds individual repeated measurements as observations when the producer provides them; an aggregate-only result is valid.

One attempt may report several quantities. CPU time, GPU time, peak memory, and a correctness metric are separate scalar results with separate units, joined by a shared `attempt_key`. A retry is a new attempt with a new key.

> **Reported coordinates** are the facts a producer reports about what was measured and under which conditions: project, designated source, benchmark identity, workload variant, subject, quantity, method, environment, and optional resource selection. Every coordinate is *constitutive*: it says what system and method produced the number and stays fixed for the attempt. They exclude the designated source revision and all provenance such as attempt/run keys and timestamps.

> A **projection** gives each reported fact a role: the **axis** a series follows, facts that must **match**, as is or normalized, and facts it **ignores** (§4.3). **Comparison identity** is what a projection requires to match. Producers report coordinates; they never declare identity or series membership.

> A **series** is one comparison identity under one projection, through one estimator, ordered along the projection's axis:
>
> **projection(reported coordinates) × estimator**

A project's **identity policy** is the projection the store maintains on ingest; its axis is the designated subject revision. Any other projection, such as a sweep across kernels or a comparison profile, yields series of the same form over the same results. One observation batch feeds a mean series and a median series without being stored twice (§4.5).

Two sources appear on every result. The top-level `source`/`revision` identify the subject component whose revisions form the history axis. `benchmark` identifies the code that defined and executed the workload; its exact revision is a reported coordinate that the identity policy normally maps to a declared benchmark version or an audited continuity mapping rather than splitting history on every benchmark commit. A composite subject such as Arrow–NumPy interoperability names every component, designates one as the axis, and pins the others (§4.2).

A result records two kinds of fact, and where a fact goes follows its kind, never the role it plays in a comparison (§4.2). **Coordinates** are constitutive: which code, build, hardware, kernel, C library, runtime, timer, and calibration rule produced the number. A different value means a different system, not the same system in a different state. **Measurements** are what the attempt produced or passed through: the measurand, and conditions such as temperature, load, clock frequency, or throttle events, each a sibling result for its own quantity sharing the `attempt_key` (§4.4).

Which facts a series follows, holds fixed, or lets vary is the **role** a projection gives them, never where they are stored. A Parquet-read series may let glibc vary while a `malloc` throughput series fixes it because it is the thing under test; both results record glibc in the same place.

For example, one series could track:

- project: Apache Arrow benchmarks; subject/source: Apache Arrow from `https://github.com/apache/arrow`;
- workload variant: Parquet read with Snappy compression;
- quantity: wall time in seconds;
- estimator: mean;
- subject configuration: release build with Clang 18;
- benchmark identity: the suite source/revision;
- method: protocol version and the warmup/calibration strategy;
- environment: runner `bench-01` with a specified CPU and core policy, running Python 3.12, kernel 6.8, and glibc 2.39; resource selection: absent, or a GPU UUID/device index for a per-device result.

Each subject revision contributes zero or more results, producing the history shown in a benchmark chart. Under the default identity policy, changing a workload parameter, quantity, method value, environment identity, or Python version yields a different series; a different estimator yields a different series over the same coordinates; changing the attempt/run key, timestamp, kernel, or glibc version does not.

The common operations are to find a series, order its results by revision, plot estimates, inspect observation batches, group an attempt's quantities, and trace a result to its producer. The indexed core is therefore scalar; richer data remains representable without first-class tables.

## 2. Vocabulary

| Term | Meaning here |
|---|---|
| **Run** | Orchestration-level grouping created by a scheduler, CLI, CI job, or similar work-order author. |
| **Attempt** | One execution request and outcome for one workload variant; it may complete, fail, or be skipped. A retry is a new attempt. |
| **Workload** | Named benchmark operation before its parameter domain is expanded. |
| **Parameter assignment** | One concrete value for each parameter of a workload, such as `{"size": 100}` from a domain of `size = [10, 100, 1000]`. |
| **Workload variant** | A workload plus one parameter assignment. |
| **Subject** | Implementation or system under test. It may contain several named components; one source is the revision axis. |
| **Quantity** | Named scalar output with a canonical unit, such as `wall-time` in `s` or `peak-rss` in `By`. An open, project-extensible vocabulary (§4.4). Canonical units use UCUM when representable. |
| **Estimator** | Rule producing a scalar estimate, such as mean, median, minimum, slope, a percentile, a function of several input quantities, or a source-defined value. Selected by policy as a series projection; reported only inside estimate summaries, never as a coordinate. |
| **Reported coordinates** | Producer-reported constitutive facts describing what was measured, by which method, on which system, fingerprinted before any projection is applied. |
| **Method** | The measurement method as executed: protocol, timer, warmup, calibration, repetition and cache policy, probe, schedule. Defines what the number is; a coordinate. |
| **Condition measurement** | A result for a state the system passes through during an attempt, such as temperature or load, sharing the measurand's `attempt_key`. An ordinary result; never a coordinate. |
| **Projection** | Gives each reported fact a role, `axis`, `match`, `normalize`, or `ignore`, scoped by workload and quantity, with optional conditions and invariants (§4.3). Roles are never expressed by placement. |
| **Identity policy** | The projection a project's store maintains on ingest; its axis is the designated subject revision. |
| **Comparison identity** | The facts a projection requires to match. |
| **Series** | One comparison identity × one estimator under one projection, ordered along its axis. It exists from its first point; a comparison between points is a separate document (§5.5). Materialized as an index; never declared by producers. |
| **Observation** | One measured value produced by a repetition after declared normalization. |
| **Observation batch** | Observations of one quantity from one attempt. Three repetitions producing 1.0 ms, 1.1 ms, and 1.0 ms are three observations in one batch. |
| **Estimate** | Scalar produced by applying an estimator to an observation batch, used for plots and analysis; not a known “true value.” Producer estimates are typed summaries on the immutable result; derived estimates are series points. |
| **Statistic** | Numeric summary computed from observations, such as a mean, median, percentile, or standard deviation. |
| **Precision statistic** | Statistic describing observation dispersion, such as standard deviation, MAD, or IQR. |
| **Measurement uncertainty** | Uncertainty associated with an estimate. It is not synonymous with error or observation spread. |
| **Environment** | Host or allocation shared by an attempt's results: identity fields that name it, and facts about its system such as kernel, C library, and runtime. |

In metrology, the quantity intended for a workload variant at a revision is the **measurand**; it needs no database entity here. Use `accuracy`, `precision`, `error`, `bias`, `confidence interval`, and `coverage interval` only with their defined meanings, and preserve source terminology when its exact meaning is unknown.

## 3. Design rules

1. **Producers report facts; projections derive identity.** A result carries reported coordinates, never a comparison identity or series identifier (§4.3).
2. **The designated subject revision is the identity policy's axis.** Ingest time is not historical order; dirty working-tree state is a per-result flag, never a revision property (§4.1).
3. **One result contains one quantity and unit.** A multi-output attempt emits several results joined by `attempt_key`; tuple outputs are split into named scalar quantities.
4. **Placement records what a fact is; projections decide its role.** A constitutive fact is a coordinate whether or not anything matches on it; a condition the attempt passes through is a sibling measurement. Changing which facts split history is a new identity policy, never a change to results or mappings (§4.2, §4.3).
5. **Environment and selected resource are explicit.** Environment identifies the shared host; optional resource selection identifies the CPU set, GPU, or partition one result applies to.
6. **Observations are preferred, not required.** Aggregate-only history remains valid and is marked producer-supplied.
7. **Summaries are typed.** Estimates, statistics, uncertainty, intervals, and opaque source bounds remain distinct entries (§5.3).
8. **Canonical units are interoperable.** Use UCUM case-sensitive units when representable, normalize values before storage, and keep the original unit in provenance.
9. **Ingestion is idempotent and auditable.** Producer identity is stable and namespaced; its software version is not part of the idempotency key (§5.4).
10. **Grouping keys are provenance.** The run author defines `run_key`; the executor defines a namespace-qualified `attempt_key`; neither enters identity.
11. **Decision policy is separate.** Direction, thresholds, guard bands, correctness verdicts, and alerts never enter identity or imply a verdict.
12. **The quantity vocabulary is open.** Adding a quantity is configuration, not a schema change; a quantity's meaning and unit are immutable once used (§4.4).
13. **Results record what was, not what was asked.** A coordinate is the value read from the attempt. The request lives in the work order, which the result cites; a value that cannot be read back is recorded as requested with a warning (§4.2).

## 4. Minimal schema

### 4.1 Core tables

| Table | One row represents | Key or invariant |
|---|---|---|
| `project` | namespace for project-defined names and policy | unique project key |
| `source` | versioned code or data source, usually a repository | unique `(project_id, canonical_uri)` |
| `workload_variant` | workload plus one resolved parameter assignment | unique `(project_id, name, parameters)` |
| `quantity` | named scalar output with canonical unit (UCUM unit when representable) and descriptive metadata (§4.4) | unique `(project_id, name)`; immutable after use |
| `environment` | host/allocation named by its identity fields; its system facts stay on each result's coordinates | unique `(identity_schema, fingerprint)` of the identity fields |
| `revision` | source revision plus the optional `native_order` and `native_time` reported with it | unique `(source_id, revision_key)` |
| `series` | derived index: one comparison identity × one estimator under one projection, with its axis and the ingest watermark it was built from | unique `series_fingerprint`; rebuildable |
| `series_point` | one entry of a series: the estimate one result contributes at its axis value, recording which result and which series | unique `(series_fingerprint, producer, ingest_key)`; rebuildable |
| `benchmark_result` | immutable producer record for one attempt × one quantity | unique `(producer, ingest_key)`; indexed by `reported_coordinates_fingerprint` |

`workload_variant.parameters` is a canonical JSON object holding one parameter assignment. A workload declared with `size=[10, 100, 1000]` yields three variants; a parameter value may itself be an array, such as a matrix shape, and the benchmark definition resolves the distinction before ingestion. A workload may also carry a `dataset` object with name, version, checksum, and generation parameters; it is part of the variant identity, so two runs on different data never share a variant. A baseline-versus-variant study (UC-02) is two variants that differ in a `role` parameter, not a separate field.

`source` and `revision` serve both subject and benchmark code. A revision key has no intrinsic order, and the revision graph is not a core table: history queries take ancestry from the source repository or from an auxiliary revision catalog keyed by `(source_id, revision_key)`. When neither is available, ordering falls back to the `native_order` or `native_time` a producer reported with the revision, as migrated LNT or rustc-perf results do; that yields a linear order but not ancestry, so ancestor-based baselines still need a graph. Without any of these, results stay queryable but revision order is unavailable. Parent hashes are not copied into results.

A `revision` row is keyed by the clean VCS identifier. Whether the measured checkout had uncommitted changes is a fact about the result: `subject_dirty` and `benchmark_dirty` are required tri-state flags, `clean`, `dirty`, or `unknown`, in provenance. A clean and a dirty result at the same commit therefore share a revision row. `unknown` is distinct from `clean` so that a comparator can tell what is known to be clean. The flag, the working-tree id below, and a quality warning exist so that a detector or comparator can filter or annotate dirty results trivially; what it does with them is its own policy. The failure mode to avoid is the one asv has, where uncommitted changes are silently ignored and a result is charted as if it were the commit. Comparators may compare dirty sides within one run, as a contributor does with an uncommitted change against HEAD; such a comparison is *local-only* and never promotes into tracked history (§5.5).

**Working-tree identity.** A result also records `subject_tree` and `benchmark_tree`: the content hash of the checkout that was measured, independent of its commit. For git this is the tree object id of the working tree, the same id a commit made from this checkout on this machine would carry. It is obtained by staging everything into a scratch index that does not yet exist, so the repository's own index is untouched, and writing a tree:

```sh
index=$(mktemp); rm -f "$index"
GIT_INDEX_FILE=$index sh -c 'git add -A && git write-tree'
rm -f "$index"
```

Tracked and untracked files count; gitignored files do not. A clean checkout yields the tree of its HEAD; a dirty one yields a tree no commit has yet. Two results with equal tree ids measured the same code whatever their commit or timestamp, so dirty runs are distinguishable from each other and from HEAD, and when that state is later committed the commit's tree equals the recorded id, so a comparator can attach the earlier results to the real revision.

Two limits follow from how git hashes a tree. Submodules and nested repositories contribute only their HEAD commit id, so their own uncommitted changes are invisible; a pinned component that lives inside the tree needs its own tree id when that matters. And what enters the hash depends on the repository's normalization settings, such as line-ending conversion, file-mode tracking, and clean filters, so ids from different producers are equal for identical code only when those settings agree. The doc does not mandate settings; equality across producers is a property of matching configuration, not of the id. The tree id identifies the code but does not reproduce it; a patch, when kept, is a provenance artifact of kind `patch`. Non-git sources use their native tree hash or a hash of sorted file paths and contents; `unknown` dirty state pairs with an absent tree id.

**Benchmark code without a revision.** Benchmarks are not always under version control. A suite installed into the environment being measured, such as a `pip install pyperformance`, has files but no revision, and a benchmark directory vendored into an image has none either. `benchmark.revision` is therefore optional when `benchmark_tree` is present: the tree, a content hash of the benchmark files, identifies the code instead, and `benchmark_dirty` is `dirty`, because an unversioned tree cannot be shown to be clean. Such a result still names its benchmark code, so it is not thin on that account; what it cannot do is have its revision mapped to a declared version. A projection may match the tree or ignore benchmark identity instead.

**Thin local results.** An ad hoc run on a laptop (UC-01, one-off stories) may have no benchmark repository and no project. `project` and `benchmark` are therefore optional, and environment identity may use the `local/v1` schema with only a hostname. Such a result is *thin*: it validates and can be compared within its own run. On ingest the store assigns it the implicit project `local/<hostname>` so that its vocabulary rows exist, and it joins a tracked project's series only after an approved mapping re-projects it with the missing coordinates (§6). When the logic under test lives in the benchmark scripts themselves, as in a Narwhals overhead study, the script repository is the subject: `source` names it, and `benchmark` either names the same source or is omitted.

`benchmark_result` stores coordinates exactly as reported plus their fingerprint, `attempt_key`, the outcome, observation batch, producer summaries, procedure, quality, and provenance as validated JSON. All of it is immutable after ingest. Promote a JSON structure to a child table only when a concrete query, size, or integrity requirement justifies it. No core tables exist for estimator, unit, procedure, run/attempt, observation, interval, uncertainty component, covariance, revision graph, or aliases; estimator and unit vocabularies are configuration.

`series` and `series_point` are materialized indexes. A series is the ordered list of estimates for one comparison identity and one estimator, for example the minimum wall time of a Parquet read on one runner across three revisions, `[50, 10, 25]`. A series point is one entry of that list, `50`, and records the result it was computed from and the series it belongs to, so either can be reached from the other. On ingest the server applies the identity policy, creating a series per comparison identity and estimator and a point per eligible result; other projections are materialized the same way (§4.3). Rebuilding under one projection never deletes series built under another. Stable annotations and external references use `series_fingerprint`, not a row ID.

A composite subject keeps the axis explicit and pins every non-primary component:

```json
{
  "name": "arrow-numpy-interop",
  "components": [
    {"role": "primary"},
    {"role": "numpy", "source": "https://github.com/numpy/numpy", "revision": "v2.3.0"}
  ]
}
```

The primary component is the one whose code the benchmark run installs from the checkout under test, so its revision changes on every commit. That revision is the history axis: it is never part of comparison identity, and it is what a chart of the series follows. A pinned component is one the run installs at a fixed version; that version is a reported coordinate. In a Narwhals overhead study run from the Narwhals repository, `narwhals` is primary and `pandas` is pinned at `v2.2.3`, so every Narwhals commit adds a point to the series `truncate × pandas v2.2.3`.

A result pins the components its workload exercises and no others. The pandas overhead benchmark pins pandas, the Polars one pins Polars, and a Polars release therefore does not touch the pandas series. The pin is a reported fact; whether a new pandas release starts a series, joins a release line, or is ignored is the role a projection gives it (§4.3), so a later projection can split what an earlier one merged.

### 4.2 Field placement

Every reported fact is placed by what kind of fact it is, and never by the role it plays in a comparison. The test is a question about the system: **if this value were different, would it be a different system, or the same system in a different state?** A different system makes the fact a coordinate; the same system in another state makes it a measurement. The value's type does not decide: core count and a locked GPU clock are numeric coordinates, a timer name and a glibc version categorical ones; temperature is a numeric measurement, a throttled-or-not flag a categorical one.

| Destination | Put here | Examples |
|---|---|---|
| **Workload-variant parameters** | Resolved controlled workload/input, including dataset identity and any parameter set from outside the harness | size, compression, scenario, role, dataset name/version/checksum, `OMP_NUM_THREADS` |
| **Subject descriptor** | What is under test: components, roles, pinned non-axis revisions, build/configuration | Arrow–NumPy roles, build type, compiler/flags, JIT/AOT mode, backend |
| **Benchmark identity** | Exact code that defined/executed the workload | source URI, revision |
| **Method** | The measurement method as executed: the rules that define what the number is | protocol version, warmup/GC/calibration policy, adaptive vs. pedantic timing, timer, CUDA events vs. synchronization, cache-clearing policy, probe and its sampling, interleaving schedule |
| **Environment identity** | Fields that name the host or allocation shared by an attempt | runner, CPU model, available accelerators, memory, cluster shape |
| **Environment facts** | Other constitutive facts about the system during the attempt | OS and kernel, C library, microcode, drivers, governor/boost/SMT settings, locked clocks, image digest, language runtime such as the Python version, the allowlisted environment of the measured process, context-collector facts |
| **Resource selection** | Optional resource one result applies to | CPU/core set or affinity mask applied, GPU UUID/device index, accelerator partition |
| **Condition measurements** | States the system passes through during the attempt, each a sibling result for its own quantity sharing `attempt_key` (§4.4) | temperature, load average, observed clock frequency, throttle events |
| **Result procedure** | What the method did in this attempt and the limits imposed | repetitions completed, inner iterations selected, warmups performed, round and slot within the run, durations, caches actually cleared, timeout imposed |
| **Provenance** | Audit metadata | run/attempt keys, caller labels, dirty flags and working-tree ids, CI link, logs, runner software, the work order that requested the attempt, references to input or anchor results, patch and dependency manifest artifacts, source payload |

Four rules resolve most cases:

- **Constitutive versus state.** Apply the question above. A setting and the state it controls are two facts and land in two places: a locked GPU clock is an environment fact and the frequency observed during the attempt a condition measurement; a cache-drop policy is method and the caches actually dropped are procedure. Temperature or load *before* the attempt is still state, an initial condition nobody fixed, so it is a measurement taken at that time.
- **Subject, method, or environment.** A fact about the artifact under test belongs in the subject descriptor, one about how it is measured in method, one about the system it ran on in the environment. All three are coordinates; the split exists for readability and adapter consistency, a fact goes in exactly one, and none of them implies a role.
- **Executed versus requested versus realized.** A coordinate records what the attempt ran with, read from the attempt where possible. What was requested lives in the work order, which the result cites as a provenance artifact (`runner-schema.md` §5.1). A fact that cannot be read back is recorded as requested and the result carries a warning such as `build-config-unverified`; a read value that differs from the request is recorded as read and the result carries `requested-value-mismatch`. What the method produced in this attempt, such as the loop count an adaptive calibration chose, the warmups performed, or whether a requested cache drop ran, is procedure. Per-observation ordinal, group, pair, and inclusion data belongs on structured observations, not in procedure.
- **Coordinate versus provenance.** A fact belongs in provenance only when it traces a result to its run, logs, and inputs and says nothing about the system or method, such as a CI link or a run key. Whether a coordinate must match is never decided by moving it (rule 4).

**Parameters set outside the harness.** A parameter need not come from the benchmark's own parameter domain. A runner may set environment variables for the measured process, or pass parameters of its own, to choose which variant is measured; these are workload-variant parameters, reported under the names used to set them. Thread control is the common case and has no single spelling (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and others), so the names are reported as written rather than folded into one canonical `threads` parameter. A variant that capped two libraries is not the variant that capped three, and reporting the names is what keeps that difference visible instead of merging two measurements into one series. This is also what lets a project compare one thread against ten within a single suite: the two are ordinary variants, as a size of 10 and a size of 1000 are. A name the harness reports for itself, such as Google Benchmark's `threads:N`, must not arrive twice with conflicting values; the adapter resolves that before ingest.

**The environment the measured process ran in.** A runner inherits the shell it was started from, so the measured process sees more than whoever ordered the run named. The environment actually passed to it is recorded per result as the environment fact `process.environment`: the variables that select the variant are already parameters, and this record exists so that a variable nobody ordered is visible after the fact. It is the environment of the measured process, whatever process model the harness uses internally, since a harness that spawns workers must give them the same environment for the record to be true. Only allowlisted variables are recorded, as in `benchmark-environments.md` §3.1, because an inherited environment carries tokens and credentials in CI and a result store is shared. This applies to every artifact the result references as well: a capture of the whole environment kept as an artifact is the same exposure.

**Naming the subject.** `subject.name` is a naming decision, and it belongs to whoever ordered the run rather than to the producer. A producer that derives it from the source URI collides whenever one repository holds several subjects, since a C++ library and its Python bindings derive to the same name, and splits one subject's history across a fork, mirror, or rename. Producers are expected to be told the name; derivation is a fallback for ad hoc results.

The top-level `source` is the authoritative axis. The subject component with role `primary` may omit its `source`; if present it must match, and a mismatch is an identity violation (§5.4). Non-primary components carry a source URI and pinned revision as fixed coordinates.

**Environment setup is out of scope.** How the subject and its pinned components are obtained, built, and installed, such as a PyPI wheel versus a source build with particular flags or BLAS backend, is the job of whoever prepares the environment; the runner never builds or installs. A pinned revision identifies code, not the installed artifact, so the runner or adapter records the facts about the environment it knows to matter: in the subject descriptor when they describe the artifact under test (install method, build type, compiler flags), as environment facts when they describe the system around it, and as a dependency manifest artifact in provenance when a full inventory is wanted. This document prescribes none of those facts and does not aim at reproducing an environment; it records enough to tell two environments apart.

A runner-level **probe** maps to a quantity plus the instrumentation used to acquire it, recorded as `method.protocol.probe`: `GPUTimeProbe` might yield `gpu-time` via CUDA events, `OSSMemoryProbe` yield `peak-rss` via a named OS counter, a thermal sensor yield `cpu-temperature`. Probe names are not a schema vocabulary. Method, environment facts, and procedure are open objects; Appendix B lists recommended keys so adapters spell the same warmup, calibration, timer, thread, cache strategy, or system fact the same way and land in the same series.

The use cases in [`docs/use-cases/`](../use-cases/) and their [template](../use-cases/UC_NN_TEMPLATE.md) reason in four coordinates. They map onto this schema as follows; note that the template's "environment" includes dependency versions, which are pinned subject components here, while `environment` in this document means the host.

| Use-case coordinate | Schema placement |
|---|---|
| Code identity | workload variant and its parameters/dataset; benchmark identity |
| Code version | subject revision, dirty flag, and working-tree id; subject configuration; pinned non-primary components |
| Environment | environment identity and facts (OS, microcode, runtime); resource selection; condition measurements for load and temperature |
| Execution context | method; procedure (`round`, `slot`); structured observation keys; `run_key` and caller labels |

### 4.3 Fingerprints and projections

The **reported-coordinate fingerprint** preserves the complete tuple before any projection: top-level project/source, the exact `benchmark` identity, and the `coordinates` object of §5.2. It never contains row IDs, an estimator, the axis revision, or attempt/run keys.

```text
reported_coordinates_fingerprint = SHA-256("benchmark-reported-coordinates-v2\0" + JCS(reported_coordinates))
```

A **projection** (`schemas/projection/0.1.0`) turns stored results into series by giving each reported fact a role:

- `axis`: the fact a series follows, with its order (revision graph, version, numeric, time, or unordered); exactly one per result;
- `match`: equal values are required to share a series;
- `normalize`: match after a normalization, such as a canonical source URI, an exact benchmark revision mapped to a declared version, or a version cut to its release line;
- `ignore`: the fact stays on every result and is shown beside the series, but never splits it.

Facts are addressed by JSON Pointer, and roles are scoped by project, benchmark, subject, workload, and quantity, the most specific scope winning, because whether a fact matters depends on what is measured: an allocator workload fixes the C-library version that a Parquet workload ignores. A projection may also keep only results whose sibling measurements satisfy a **condition**, such as zero throttle events. Conditioning on a state the workload itself drives, such as temperature under a heavy kernel, selects on a consequence of the thing measured and can bias the series; a state the workload does not cause, such as load from other processes, is safer. A projection whose axis is not the code must match the subject tree, or declare `mixed_code` when mixing code is the point, as in a trend over time.

A project's **identity policy** is the projection the store maintains on ingest; its axis is the designated subject revision. The default (`examples/default.json`) matches every coordinate except environment facts, of which it matches only the runtime: what the former split between comparison context and observed context did, as a default a project can override per scope. One behaviour changes: a thread cap inherited from the shell, once an intended setting that split history, is now the ignored fact `process.environment` (`benchmark-environments.md` §9 question 2).

Any other projection, such as a sweep across kernels (`examples/kernel-sweep.json`) or a comparison profile (§5.5), is materialized over the same results into series of the same form, by the store on request or by a consumer, and records the ingest watermark it was built from so a reader can tell when it is stale. The `identity_schema` names a projection and changes whenever its semantics or continuity mappings change, so series of different projections, or of two versions of one, never mix. Continuity mappings, which keep history across a benchmark rename, an added parameter, or a machine upgrade, are applied in this step; their shape, authoring, and audit trail are specified in a separate design document.

```text
comparison_fingerprint = SHA-256("benchmark-comparison-v1\0" + identity_schema + "\0" + JCS(comparison_identity))
estimator_fingerprint  = SHA-256("benchmark-estimator-v1\0" + JCS(estimator))
series_fingerprint     = SHA-256("benchmark-series-v6\0" + comparison_fingerprint + "\0" + estimator_fingerprint)
```

`JCS` is RFC 8785 canonical JSON. The server computes all fingerprints; canonical source URIs, number/string normalization, omitted values, estimator method versions, and projection definitions are part of the contract.

### 4.4 Quantity vocabulary

Quantities are an **open vocabulary**: wall time is not privileged, and the schema never enumerates what may be measured. CPU, GPU, and wall time; three distinct memory semantics; hardware counters; throughput; energy; binary size; quality scores are all ordinary quantities. Appendix A gives non-normative examples.

A quantity declaration carries:

- **`name`**, stable within the project and immutable once used. Distinct semantics need distinct names: bytes allocated, returned-object size, and peak RSS are three quantities, never one `memory`.
- **`unit`**, the canonical UCUM unit (rule 8). Counts are `1`; rates are unit-per-time such as `By/s`. Synthetic units are named as what they are: a simulator's weighted cycle estimate is `estimated-cycles` in `1`, never seconds.
- **`direction`** *(optional)*: `lower-is-better` or `higher-is-better`. Recorded because producers know it; excluded from identity and verdicts (rule 11).
- **`deterministic`** *(optional)*: repetition under fixed conditions reproduces the value, as with simulated instruction counts or binary size. One observation is then complete evidence, not a weak sample.

Percentiles are estimators, not quantities: tracked p99 latency is quantity `latency` with estimator `p99`, one series per percentile.

**Condition measurements.** A state the system passes through during an attempt is a sibling result for its own quantity, sharing the measurand's `attempt_key`, so attempt integrity (§5.3) ties it to the same workload variant, subject, and environment. `method.protocol.probe` names the sensor and how it was read (Appendix B); sampled values are structured observations carrying `time`, aligned with the measurand's. A probe that cannot read yields status `skipped` with reason `probe-unavailable` and never touches the measurand's result. Whether a quantity is "the thing measured" is a question of the projection, not of the data: `examples/arrow-temperature.json` has its own series, showing thermal drift, and can condition the wall-time series of `examples/arrow.json` (§4.3).

### 4.5 Estimators and compound outputs

An estimator declaration has a name, method version, optional parameters that can alter the value such as percentile interpolation, and an input level of `observations` or `estimates`. A projection lists the estimators it materializes. For every result with sufficient observations, the server writes a derived point per server-computable listed estimator; a result with only producer estimates joins only the series of the estimators it reported. Adding an estimator later creates points for results with observations and never rewrites a stored result.

A **compound estimator** additionally declares semantic input roles and quantities, which enter `estimator_fingerprint`. A separate `provenance.references` list maps roles to stable `(producer, ingest_key)` input references, which do not enter identity. Inputs may be any results in the same `run_key`: siblings of one attempt, as for a CPU/GPU critical-time; or results of different attempts, as for a Narwhals-over-pandas overhead ratio across two role variants or an anchor measurement in a cross-machine study. Explicit references remove ambiguity when a run holds several candidates. Paired observations carry matching group/ordinal or pair keys.

```json
{
  "estimator": {
    "name": "max",
    "method_version": "benchx/max/v1",
    "input_level": "estimates",
    "inputs": [{"role": "cpu", "quantity": "cpu-time"}, {"role": "gpu", "quantity": "gpu-time"}]
  },
  "references": [
    {"role": "cpu", "result": {"producer": "example.org/adapter", "ingest_key": "ci-1234:parquet-read/snappy:cpu-time"}},
    {"role": "gpu", "result": {"producer": "example.org/adapter", "ingest_key": "ci-1234:parquet-read/snappy:gpu-time"}}
  ]
}
```

**How a compound result is created.** A compound is an ordinary result for its own quantity, produced by a short pipeline that runs after its inputs exist and reads only stored, immutable results:

1. **Resolve inputs by role** within one `run_key`, and record each as a `(producer, ingest_key)` reference in `provenance.references`.
2. **Check compatibility.** Inputs must share what the compound's series will claim: subject revision and tree, pinned components, environment identity and facts, method apart from quantity-specific instrumentation such as `protocol.timer`, `protocol.synchronization`, and `protocol.probe`, and, across attempts, `round`. Instrumentation may differ per input, as it does between CPU and GPU time; it stays on the referenced inputs, and the compound's own method carries only the shared keys. Attempt integrity (§5.3) guarantees this for same-attempt inputs; the consuming profile (§5.5) does for cross-attempt ones.
3. **Pair observations** by ordinal or pair key. Pairing needs equal counts, which is why the profiles require interleaved rounds and equal repetitions.
4. **Apply the derivation rule per pair**, such as `max` of CPU and GPU time or the ratio of narwhals to native time. The results are the compound's observation batch, kept as structured observations with their ordinals, and the rule is declared next to them in `measurement.derivation` as an estimator at `input_level: observations`.
5. **Apply an ordinary estimator** to that batch, `median` say, as the producer estimate. A projection may materialize other estimators from the same batch later.
6. **Assemble coordinates**: those the inputs share, minus those they differ in such as `parameters.role`, plus the compound's own quantity. A same-attempt compound keeps the input `attempt_key`; a cross-attempt one gets a new attempt key in the same run.
7. **Ingest** as a normal result. The inputs are untouched.

Because the inputs keep their batches, the pipeline runs identically at measurement time in the runner or later in the comparator, and the compound can be added retroactively across a project's whole history. The comparator has two outputs: a stored compound, ingested under its own producer name, which gets a series with one point per revision and is the form a tracked question such as "does the overhead stay low across commits" needs; or a value inside one comparison document, with no series, which is the form for a one-off question. Same inputs and the same versioned declaration land in the same series whoever computed it; a different rule or estimator parameter is a different series beside the old one, never a rewrite.

A compound over paired inputs must carry the pairwise batch. `input_level: estimates` is permitted only when the inputs have no observations or do not pair, and it yields an estimate without a batch. A statistic that is not a per-pair rule, such as a ratio of two minimums, is not a compound batch at all; it is a comparison-time statistic over the two input series, or a separate estimate-level compound with its own series. `examples/narwhals-overhead-ratio.json` shows the tracked form.

The indexed contract remains scalar: `median([cpu_times, gpu_times])` emits one `cpu-time` and one `gpu-time` result rather than a tuple. Vectors, covariance, and other multidimensional evidence remain artifacts (§7).

## 5. Result contract

### 5.1 Fields and statuses

| Group | Fields |
|---|---|
| **Identity facts** | reported source, `coordinates`, `reported_coordinates_fingerprint`, subject `revision_key`; project and benchmark identity when not thin |
| **Attempt grouping** | `attempt_key` |
| **Outcome** | `status`, optional reason, optional quantitative constraint |
| **Producer evidence** | optional observation batch and immutable typed summaries |
| **Procedure** | optional realized procedure JSON, including `round` within the run |
| **Sibling measurements** | not fields: condition measurements are separate results sharing `attempt_key` (§4.4) |
| **Provenance** | producer/version, ingest/native/run/batch keys, caller labels, dirty tri-state flags and working-tree ids, references to input or anchor results, mapping version, timestamps, artifacts, payload URI/checksum |
| **Quality** | immutable producer validation, including an observed correctness outcome, and warnings; store-side quarantine/exclusion is separate state |

Derived `series_point` rows live outside the result. Statuses are:

- `success`: a usable observation batch or at least one usable producer estimate;
- `partial`: the same evidence plus a reported partial failure and reason;
- `censored`: only a lower, upper, or interval constraint is known, such as `runtime > 60 s`;
- `error`: no usable estimate or constraint;
- `skipped`: the harness explicitly skipped this known workload variant.

Absence of a row means the variant was not reported. Telling a lost result from one that was never planned requires the run manifest, which belongs to the runner and scheduler design and is out of scope here. Detectors exclude partial, censored, error, skipped, and quarantined results by default; how they treat results not marked `clean` is their policy, informed by the dirty flag and tree id (§4.1).

### 5.2 Example ingest object

```json
{
  "schema_version": 6,
  "producer": {"name": "example.org/benchmark-adapter", "version": "4.0", "mapping_version": "native-to-benchx/v5"},
  "ingest_key": "ci-1234:parquet-read/snappy:wall-time",
  "attempt_key": "ci.example.org/runs/ci-1234/attempts/parquet-read-snappy-1",
  "project": "arrow",
  "source": {"uri": "https://github.com/apache/arrow", "type": "git"},
  "revision": {"key": "02addad..."},
  "benchmark": {
    "source": {"uri": "https://github.com/example/arrow-benchmarks", "type": "git"},
    "revision": {"key": "92ef3aa..."}
  },
  "coordinates": {
    "workload": {"name": "parquet-read", "parameters": {"compression": "snappy"}},
    "subject": {
      "name": "apache-arrow",
      "components": [{"role": "primary"}],
      "configuration": {"build_type": "release", "compiler": "clang-18"}
    },
    "quantity": {"name": "wall-time", "unit": "s"},
    "method": {
      "protocol": {
        "name": "benchmark-time",
        "version": "v1",
        "timer": "perf_counter",
        "warmup": {"mode": "time", "seconds": 1},
        "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.01},
        "filesystem_cache": "drop-before-attempt"
      }
    },
    "environment": {
      "schema": "machine/v1",
      "identity": {"runner": "bench-01", "cpu": "AMD EPYC 7R13", "cores": 8},
      "facts": {
        "os": {"kernel": "6.8.0-31-generic"},
        "libc": {"version": "2.39"},
        "runtime": {"python": "3.12"}
      }
    }
  },
  "measurement": {
    "status": "success",
    "observations": [0.0371, 0.0364, 0.0363, 0.0370, 0.0368],
    "summaries": [
      {
        "type": "estimate",
        "estimator": {"name": "mean", "method_version": "benchx/mean/v1", "input_level": "observations"},
        "value": 0.03672,
        "source": "producer"
      },
      {
        "type": "statistic",
        "name": "sample_standard_deviation",
        "value": 0.0003564,
        "method": "n-1",
        "source": "producer"
      },
      {
        "type": "confidence_interval",
        "statistic": "mean",
        "level": 0.95,
        "lower": 0.03628,
        "upper": 0.03716,
        "method": "student-t",
        "source": "producer"
      }
    ]
  },
  "procedure": {
    "inner_iterations": 100,
    "attempted_repetitions": 5,
    "completed_repetitions": 5,
    "warmups_performed": 1,
    "caches_cleared": ["filesystem"]
  },
  "provenance": {
    "run_key": "ci.example.org/runs/ci-1234",
    "started_at": "2026-07-18T09:12:44Z",
    "subject_dirty": "clean",
    "subject_tree": "4b825dc...",
    "benchmark_dirty": "dirty",
    "benchmark_tree": "9fceb02...",
    "artifacts": [
      {"kind": "patch", "media_type": "text/x-diff", "uri": "file:///tmp/bench/benchmark.patch", "sha256": "aaaa..."}
    ],
    "runner": {"name": "example-runner", "version": "2.1"},
    "source_payload_sha256": "bbbb..."
  },
  "quality": {"warnings": ["dirty-benchmark-tree"]}
}
```

A thin local message (§4.1) from a contributor's laptop omits `project` and `benchmark`, uses the `local/v1` environment schema, and carries the run-level facts the local profiles need: a `role` parameter, a pinned dependency, `procedure.round`, a caller label, and honest dirty flags. It validates against the same schema and is `schemas/measurement-result/0.1.0/examples/adhoc.json`:

```json
{
  "schema_version": 6,
  "producer": {"name": "example.org/benchx-cli", "version": "0.1.0", "mapping_version": "timeit-to-benchx/v1"},
  "ingest_key": "session-7f3a:truncate/role=variant:round-1:wall-time",
  "attempt_key": "urn:uuid:7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "source": {"uri": "https://github.com/pandas-dev/pandas", "type": "git"},
  "revision": {"key": "a1b2c3d..."},
  "coordinates": {
    "workload": {"name": "truncate", "parameters": {"role": "variant", "rows": 1000000}},
    "subject": {
      "name": "pandas",
      "components": [
        {"role": "primary"},
        {"role": "numpy", "source": "https://github.com/numpy/numpy", "revision": "v2.0.0"}
      ]
    },
    "quantity": {"name": "wall-time", "unit": "s"},
    "method": {
      "protocol": {
        "name": "timeit",
        "version": "v1",
        "timer": "perf_counter",
        "warmup": {"mode": "repetitions", "n_warmup": 1},
        "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.2},
        "repetitions": {"mode": "fixed", "n_repeat": 5},
        "schedule": {"unit": "attempt", "rule": "alternating", "rounds": 5}
      }
    },
    "environment": {
      "schema": "local/v1",
      "identity": {"hostname": "rok-laptop"},
      "facts": {"runtime": {"python": "3.12"}}
    }
  },
  "measurement": {
    "status": "success",
    "observations": [0.000131, 0.000129, 0.000133, 0.000130, 0.000128]
  },
  "procedure": {
    "inner_iterations": 2000,
    "attempted_repetitions": 5,
    "completed_repetitions": 5,
    "warmups_performed": 1,
    "round": 1,
    "slot": 3
  },
  "provenance": {
    "run_key": "urn:uuid:3f1d2c4b-8a9e-4f60-b1c2-d3e4f5a6b7c8",
    "started_at": "2026-09-14T10:02:31Z",
    "subject_dirty": "dirty",
    "subject_tree": "e7a1c3d...",
    "benchmark_dirty": "unknown",
    "labels": {"env": "numpy-2.0"}
  }
}
```

The ad hoc message has no producer summaries: the server, or the local comparator, derives the minimum and median from the observations. Its sibling with `role = baseline` shares the run key and round, and the `variants` profile pairs them. Because the subject tree is dirty, any comparison built on it is local-only (§5.5).

After accepting the Arrow message above, the server may materialize points such as the following. They are not fields on the result:

```json
{
  "series_fingerprint": "cccc...",
  "result": {"producer": "example.org/benchmark-adapter", "ingest_key": "ci-1234:parquet-read/snappy:wall-time"},
  "estimator": {"name": "median", "method_version": "benchx/median/v1", "input_level": "observations"},
  "value": 0.0368,
  "source": "derived"
}
```

### 5.3 Semantics and integrity

**Evidence**

- `summaries` is an extensible producer-evidence list with types `estimate`, `statistic`, `confidence_interval`, `coverage_interval`, `source_bounds`, `standard_uncertainty`, and `expanded_uncertainty`. Each entry records the statistic addressed, source, method, and level or factor when applicable. Values use the quantity's canonical unit unless explicitly dimensionless.
- A producer `estimate` has a finite value, a complete estimator declaration, and `source = producer`; at most one per estimator per result.
- Observations are finite values in the quantity's unit after declared normalization. A numeric array is the common batch; structured observations carry per-observation ordinal, slot, group, pair, inclusion, or exclusion data.
- Observation standard deviation is a precision statistic, not automatically the uncertainty of a mean or median. Unknown bounds stay `source_bounds`; labels such as `stat`, `sys`, `range`, or `error` keep source semantics.
- For a deterministic quantity (§4.4), one observation is complete evidence and must not be treated as an undersampled distribution.
- A limit the run author imposed on the attempt, such as a timeout per workload variant, is recorded as `procedure.timeout_seconds` whether or not it fired, so a result shows the conditions it ran under. An attempt stopped by the limit has status `error` with reason `timeout`. It is not `censored`: the limit bounds the attempt's duration, not the quantity, so a lower bound on the quantity would claim more than is known unless the quantity is the attempt's duration itself.
- `success` and `partial` require a non-empty usable batch or at least one finite producer estimate. `censored` requires a constraint with kind (`lower_bound`, `upper_bound`, `interval`), finite bounds, inclusivity, and cause. `error` and `skipped` carry none of these. Iteration and repetition counts are positive, completed repetitions never exceed attempted ones, durations non-negative, and interval lower bounds never exceed upper bounds.

**Derived projections**

- A result contributes to a series when it has a producer estimate for that estimator or enough observations for the server to derive one. The value is stored in `series_point`; a producer estimate for the same estimator stays unchanged, and a material discrepancy is recorded as a point warning.
- Producers never send a comparison fingerprint, series identifier, series point, or projection. Reported coordinates and producer evidence are immutable; derived indexes change only by adding or rebuilding materializations of versioned projections, which are retained side by side. Condition measurements never enter comparison identity by equality; a projection may condition on them (§4.3).

**Attempts and keys**

- A harness invocation reporting several quantities yields one result per quantity sharing `attempt_key`; corresponding observations share group/ordinal or pair keys.
- **Attempt integrity.** Results sharing an `attempt_key` must agree on project, designated source, subject revision, benchmark revision, dirty flags, working-tree ids, workload variant, subject descriptor, environment identity and facts, and `run_key`. They may differ in quantity, resource selection, and quantity-specific method such as timer or probe, which is what lets a temperature or load measurement be a sibling of the wall time it accompanies. Disagreement with a stored sibling is an *attempt conflict* (§5.4). Resource selection is what lets GPU 0 and GPU 1 results share one host environment.
- Two results are **code-identical** when their `subject_tree` ids match, and likewise for `benchmark_tree`. This is the comparison unit for local work: a dirty checkout against HEAD is two distinct trees, and repeated rounds of the same dirty checkout are one tree. Detection and history still use the revision axis; how a project charts results not marked `clean` is its policy, and the tree id is what lets it annotate them honestly.
- `run_key` comes from the run author, who may also attach `provenance.labels`, a string map such as `{"env": "numpy-2.0"}`, for selecting sides of a comparison. The executor creates a globally namespace-qualified `attempt_key`, such as a URI-like key or UUID, and producers preserve or deterministically map it. Keys and labels never enter identity. Identical retries of `(producer, ingest_key)` return the existing result; different payloads conflict.
- Pairing across attempts, such as interleaved baseline and contender runs (UC-03), is expressed by matching `procedure.round` on the attempts and matching pair keys on structured observations. `round` is assigned by the run author, increases monotonically within a run, and is kept by a retry, which gets a new attempt key but the same round so that pairing survives. It is never placed in method, where the default policy would split the series on it. `procedure.slot` is the attempt's position in the run's realized order across all sides, so a comparator can verify the schedule it was given, whether alternating or random, and regress estimates on slot to detect drift. When a harness interleaves at the observation level, as Google Benchmark's random interleaving does, each observation carries its own `slot` and `pair` instead. The schedule the run followed is `method.protocol.schedule` (Appendix B); the slots are the realized order.
- `provenance.started_at` is required on every result. It is the measurement start on the producer's clock and the only fact that orders attempts across runs; ingest time is never used for ordering.
- Ratios, confidence bounds, and faster/slower classifications produced by a comparison are a separate comparison document, not fields of a result; the comparator design specifies it.
- Ground-truth values never leave the runner. What is recorded is what the runner derived from them: an error norm or mismatch count as another quantity under the same attempt, and the observed check as `quality.validation.correctness` (`pass` or `fail`). The verdict that acts on it is decision policy. A full dependency manifest is a provenance artifact; the components the workload exercises are pinned in the subject descriptor (§4.1), and policy decides which pins split history.
- A result's designated source, workload variant, and quantity belong to one project, and its subject revision refers to its designated source.
- Large observation sets, covariance matrices, profiles, histograms, and similar outputs are external artifacts with media type, URI, and checksum. Migrations retain mapping version and source payload reference.

Physical partitioning and indexes are implementation choices; global idempotency must hold regardless.

### 5.4 Ingest contract

The ingest object (§5.2) is the message; transport is a deployment choice. The litmus test is **file drop**: writing result documents to a directory that is later swept into the store must be a valid delivery path.

- **Acknowledgment means validated and durable.** A positive response promises the result is stored and queryable; the producer may forget it.
- **Entities auto-create; meaning is protected.** First sight of a project, source, workload variant, quantity, or revision creates it, and series and points are materialized without changing the result. A quantity arriving with a different unit is rejected as a unit conflict, enforcing rule 12 at ingest rather than at declaration.
- **No ordering, no deadline.** Results may arrive late, duplicated, or out of order; an offline laptop or weekly batch is valid indefinitely.
- **Batches are transport optimizations.** A multi-result delivery is N independent documents with N outcomes; there is no atomic batch, and `attempt_key` survives any framing.
- **Machine-readable rejection taxonomy.** At minimum *malformed*, *idempotency conflict* (same `(producer, ingest_key)`, different payload), *unit conflict*, *identity violation* (coordinates and revision from different sources, a primary component whose source differs from the top-level source, or a payload carrying a series identifier or fingerprint), *attempt conflict* (§5.3), and *quarantined* (accepted but withheld pending review).

### 5.5 Comparison profiles

A **profile** is a built-in projection for comparing the sides of one run: its axis is the coordinate that differs between sides, it matches everything else as the default identity policy does, and its invariants are checked before any verdict. A run that fails one gets no verdict for the units it affects, and the comparison document lists every other differing fact as an unmatched difference. Each use case in `docs/use-cases/` names a profile; `schemas/projection/0.1.0/examples/revisions-profile.json` is `revisions`.

| Profile | Axis | Invariants besides `one-run` |
|---|---|---|
| `single-run` ([UC-01](../use-cases/UC-01-short-slug.md)) | workload parameters | none beyond attempt integrity |
| `variants` ([UC-02](../use-cases/UC-02-variants.md)) | the workload parameters the caller names, `parameters.role` by default; subject tree matched | `equal-attempts`, `alternating-slots` |
| `revisions` ([UC-03](../use-cases/UC_03_compare.md)) | subject tree | `two-sides`, `one-tree-per-side`, `equal-attempts`, `alternating-slots`, `clean-tree` |
| `environments` ([UC-04](../use-cases/UC-04-cross-env.md)) | one pinned component, subject configuration, or environment fact, sides named by `labels` | `two-sides`, `equal-attempts`, `alternating-slots` |
| `cross-machine` (pending a use case) | environment identity; subject tree matched | `anchor-reference` |

The axis is an argument to the comparator, not a producer declaration, so any recorded coordinate can be the varying one: `stable` for a sort-algorithm study, `{role, size}` for a two-factor design. An unplanned comparison is the same operation as a planned one, provided the run contains the sides; only a planned run guarantees the interleaving invariants.

A comparison in which any side is not `clean` is **local-only** under a profile without `clean-tree`: the comparator produces it, labels it so, and it never becomes tracked history. `revisions` has `clean-tree` because UC-03 §10 rejects dirty trees; whether it should allow a local-only comparison of an uncommitted change against HEAD is open (UC-03 §12 question 3). Thin results (§4.1) may claim `single-run`, `variants`, and `revisions` within their own run; the others require a full environment identity.

A revision may hold several attempts with identical coordinates, so viewers and comparators must expect more than one result per coordinates and revision, and a comparison document lists the `(producer, ingest_key)` of every result it consumed. That is also what lets a saved baseline be reused by reference instead of re-measured (Arrow local story). A trend over ad hoc attempts is a projection with a run or time axis and `mixed_code` (§4.3); no timestamp or counter in a result implies code identity.

## 6. Migration contract

Each import records whether observations are structured, flat, or absent; whether several quantities share a native attempt; whether benchmark and subject revisions can be distinguished; whether reported coordinates are complete for the identity policy or *thin*; and whether summaries are typed, source-defined, or absent.

*Thin* means the source lacks coordinates the target identity policy requires. The importer records the missing attributes in provenance rather than inventing defaults, and thin imports are not matched across sources without an approved mapping. An importer preserves a native attempt identifier when its grouping semantics are known and otherwise generates a distinct attempt key per result.

Importers must be idempotent, retain source payload and mapping version, report identity collisions before writing, preserve failures/skips/limits, reject unit conflicts, and use the same mapping rules as live adapters. They must not parse parameters from names without project rules or copy source-local fingerprints.

### 6.1 Tool mapping summary

| Source | Core mapping | Important caveat |
|---|---|---|
| **ASV** | hardware → environment identity; OS and interpreter → environment facts; dependencies/build → subject descriptor; benchmark + parameters → workload variant; result → **median estimate**; samples → observations; CI99 → median confidence interval | `null` is failed and `NaN` is skipped. Preserve exact benchmark version as benchmark identity; map its declared semantic version conservatively into method. |
| **Conbench** | native case → workload variant; hardware → environment; source → designated source/revision; context fields split among subject, method, and environment facts; data → observations; times → procedure | Derive quantity and estimator; do not copy `history_fingerprint`. Use the source deployment's configured summary policy. |
| **Bencher** | Project/Testbed/Benchmark/Measure → project/environment/workload variant/quantity; Metric → source-defined estimate; Report → provenance | Bounds are adapter-defined. Keep opaque bounds as `source_bounds`; threshold direction is decision policy. |
| **Codespeed** | Project/source/revision → project and designated axis; Executable → subject descriptor; Benchmark → workload variant + quantity; Environment → environment identity and facts; Result aggregates → summaries | `Benchmark.data_type` selects mean or median. Snapshot mutable unit definitions. |
| **LNT** | suite → project; Machine fields split among subject, environment identity and facts, and method; Test → workload variant; metric → quantity; list/scalar → observations/estimate; Run → provenance | Preserve native order, status/hash, and source metadata. Opaque machine identity is thin. |
| **Nyrkiö** | test path → workload variant; metric name/unit → quantity; value → source-defined estimate; source attributes → revision | Preserve per-metric direction as source decision metadata; conflicts require review. |
| **github-action-benchmark** | suite/project; name → workload variant; tool/unit → quantity; value → source-defined estimate; commit/date → revision/provenance | Interpret `range` by adapter version; otherwise retain it as an opaque summary. History may be truncated. |
| **OpenTelemetry Benchmarks** | scenario ID/workload → workload variant/method; tracked package version → revision; `name`/`unit`/`value` → quantity/estimate; `extra` → environment identity and facts/provenance | Its current output is github-action-benchmark's `{name, unit, value, extra}` format. Estimator and direction come from the scenario document; observations and uncertainty are absent. |
| **rustc-perf** | artifact → revision; pstat dimensions → workload variant/method/quantity; collection → run/attempt/observation grouping | Group compatible collection rows into observations. Environment is thin unless collector metadata can be joined. |
| **CodSpeed** | aggregate-only source estimate | No reviewed public export schema supports a migration contract. |

### 6.2 Live harness adapters

- **Google Benchmark:** iteration rows become observations; aggregate rows remain producer summaries; secondary metrics become sibling results sharing `attempt_key`; split mixed context into method, environment identity and facts, procedure, and provenance; sampled readings such as load become condition measurements.
- **pytest-benchmark:** round data becomes observations; the warmup/timing strategy it ran with goes in method, realized rounds, iterations, and warmups in procedure.
- **JMH:** preserve fork grouping; map `scoreConfidence` as a 99.9% confidence interval; mode and secondary metrics define quantity/estimator semantics.
- **cargo-criterion:** pair `iteration_count` with `measured_values`; retain typical/mean/median/MAD/slope estimates and bounds.
- **BenchmarkDotNet:** detailed measurements become observations; retain parameters, job, host, statistics, confidence interval, and percentiles with source semantics.

### 6.3 OpenTelemetry interoperability

OpenTelemetry defines no general benchmark-result convention; its benchmark repository reports through github-action-benchmark, and OTLP Metrics is a generic time-series transport. Compatibility is therefore an adapter concern.

For OTLP export, map environment to `Resource`, producer name/version to `Scope`, quantity/unit to a `Metric`, the exported `series_point` estimate to a `Gauge` point, and project/source/workload variant/method/resource selection/revision/estimator to point attributes. Export only usable scalar estimates: OTLP has no lossless representation for observations, source bounds, intervals, errors/skips, or idempotency keys, so the result store remains the system of record. Because OTLP treats attributes as stream identity, do not export facts the identity policy ignores as metric attributes when one continuous downstream series is required; put them on a linked log/span instead, and export condition measurements as metrics of their own. When ingesting OpenTelemetry CI/CD metadata, map `vcs.repository.url.full` to source URI, `vcs.ref.head.revision` to revision key, `cicd.pipeline.run.id` to run key, and `cicd.worker.id` to environment identity, preserving the original attributes and schema URL in provenance.

## 7. Scope and simplifications

The following do not justify core tables for current use cases:

- **Measurement procedure, measuring system, and model:** covered by method, environment facts, condition measurements, procedure, and provenance.
- **Runs/events:** `run_key` and `attempt_key` group results without a transactional parent.
- **Observation rows, uncertainty budgets, covariance, and multidimensional data:** inline typed JSON for the common case; artifacts for the large or specialist case.
- **Unit and estimator tables:** the quantity stores its unit; a projection lists the estimators it materializes.
- **Revision graph and alias tables:** source integrations or an optional revision catalog provide topology; audited display mappings provide aliases.

A structure becomes first-class only when it participates in comparison identity, referential integrity, or frequent indexed queries. This keeps the core at nine tables. The store also excludes alert policy, CI orchestration, source-code hashes as workload-variant identity, source-local fingerprints, large blobs, and branch/fork metadata as revision properties.

## 8. Reviewed sources

### Measurement science and experimental physics

- [JCGM 200:2012, *International Vocabulary of Metrology (VIM)*](https://www.bipm.org/documents/20126/2071204/JCGM_200_2012.pdf)
- [JCGM 100:2008, *Guide to the Expression of Uncertainty in Measurement (GUM)*](https://www.bipm.org/documents/20126/2071204/JCGM_100_2008_E.pdf)
- [JCGM 106:2012, *The Role of Measurement Uncertainty in Conformity Assessment*](https://www.bipm.org/documents/20126/2071204/JCGM_106_2012_E.pdf)
- [BIPM, *SI Brochure*, 9th edition](https://www.bipm.org/documents/20126/41483022/SI-Brochure-9.pdf)
- [HEPData data format, pinned `e54de2d`](https://github.com/HEPData/hepdata-submission/blob/e54de2d5b349f92ca4eadc5722ea99c7273d24ee/docs/data_yaml.rst)
- OpenTelemetry: [performance benchmark guidance](https://github.com/open-telemetry/opentelemetry-specification/blob/3a145b2f860d5ad94a393bc0879b309d5b8153be/specification/performance-benchmark.md), [Metrics data model](https://github.com/open-telemetry/opentelemetry-specification/blob/3a145b2f860d5ad94a393bc0879b309d5b8153be/specification/metrics/data-model.md), and [CI/CD semantic conventions](https://github.com/open-telemetry/semantic-conventions/blob/baadd5669ac53133501de100c020b4fd06533f12/docs/resource/cicd.md)

### Benchmark systems and harnesses

- [ASV result format (`7032df7`)](https://github.com/airspeed-velocity/asv/blob/7032df701a969fa61f4c819ce9f71fb2e66f5a62/docs/source/dev.rst) and [statistics (`01d4a25`)](https://github.com/airspeed-velocity/asv_runner/blob/01d4a2556932ca63e8d7df6536551c6107397e66/asv_runner/statistics.py)
- [Conbench (`3af4a55`)](https://github.com/conbench/conbench/blob/3af4a55206ad3918762cc8dd7d3012eadbe96a54/conbench/entities/benchmark_result.py), [Bencher (`d2895af`)](https://github.com/bencherdev/bencher/tree/d2895af8c867c83b8fe766a6b84d8ccd4df5c315/services/console/src/content), and [Codespeed (`263860b`)](https://github.com/tobami/codespeed/blob/263860bc298fd970c8466b3161de386582e4f801/codespeed/models.py)
- [LNT (`0e08c62`)](https://github.com/llvm/llvm-lnt/tree/0e08c627cc4804279b9439dd8ba32959cf3872f8/docs), [Nyrkiö (`5b63790`)](https://github.com/nyrkio/nyrkio/blob/5b6379026e3cb28807dd8402f4d82ce2996700db/backend/api/model.py), [github-action-benchmark (`86d8bcf`)](https://github.com/benchmark-action/github-action-benchmark/tree/86d8bcf4dc945c81ee3547d15499abafc89a57b5/src), and [rustc-perf (`3bbef83`)](https://github.com/rust-lang/rustc-perf/tree/3bbef83aea7206205fe38d7612b0a0cd6cd75ba2/database)
- [CodSpeed CPU Simulation](https://codspeed.io/docs/instruments/cpu)
- OpenTelemetry Benchmarks: [S001 scenario](https://github.com/open-telemetry/opentelemetry-benchmarks/blob/3c826853b4964593b2cdb3ffdbb84a124ca8e706/scenarios/S001-counter-increment-api-only.md) and [harness reporting format](https://github.com/open-telemetry/opentelemetry-benchmarks/blob/3c826853b4964593b2cdb3ffdbb84a124ca8e706/harnesses/README.md)
- Harnesses: [Google Benchmark (`8b66b54`)](https://github.com/google/benchmark/blob/8b66b54f7e1bf6b25390dca1dea3f18a40e607f9/docs/user_guide.md), [pytest-benchmark (`47d66c8`)](https://github.com/ionelmc/pytest-benchmark/tree/47d66c88b84b5b11cc78e465cfc655d0a02de740), [JMH (`a194eea`)](https://github.com/openjdk/jmh/blob/a194eead0136bb66e5e59e4fdb2e18543e730929/jmh-core/src/main/java/org/openjdk/jmh/results/format/JSONResultFormat.java), [Criterion (`3dbc6c6`)](https://github.com/bheisler/criterion.rs/blob/3dbc6c618acb48885066422d81d50729aa17b2b7/book/src/cargo_criterion/external_tools.md), and [BenchmarkDotNet (`2365829`)](https://github.com/dotnet/BenchmarkDotNet/blob/2365829b82d95843e561f9ef666f4e9e86761d38/docs/articles/samples/IntroExportJson.md)

The pinned primary-source links above are the review record included with this draft.

## 9. Open questions from the placement revision

1. **Unnamed environment facts.** The default identity policy ignores environment facts it does not name, so a fact an adapter starts reporting never splits history by surprise; matching them instead would make each addition visible as a split. Which failure is worse?
2. **Envelope weight.** Each condition measurement is a full result repeating the attempt's coordinates. Should one message be able to carry several sibling measurements?
3. **Addressing pinned components.** JSON Pointers address array members by index; a release-line rule for one pinned component needs to address `subject.components` by `role`.
4. **The system boundary.** For self-modifying systems, such as a warmed JIT or an on-disk kernel cache, the constitutive-versus-state test depends on whether "the system" is the code or the running process. Is a warm cache method, an environment fact, or a measured state?
5. **Projections shipped with benchmarks.** A benchmark author knows what a workload is sensitive to and could ship projections a project adopts into its identity policy. Where do they live, and how are they versioned against the benchmark code?
6. **Stale materializations.** Who rebuilds a projection materialized on top of the identity policy, and when: on read, on a schedule, or only on request?
7. **Sources of facts.** Whether a fact was declared, detected, or reported by a collector (`benchmark-environments.md` §9 question 1) has no home now that the observed-context record is retired; one option is a provenance map from JSON Pointer to source.

## Appendix A: Example quantities (non-normative)

Illustrations of the open vocabulary (§4.4), with canonical UCUM units. None of these names is required; projects declare what they measure.

| Name | Unit | Meaning | Notes |
|---|---|---|---|
| `wall-time` | `s` | elapsed real time | universal |
| `cpu-time` | `s` | process CPU time | Google Benchmark default; hyperfine user/sys |
| `gpu-time` | `s` | device-side time, synchronized | cupyx, torch benchmarks |
| `latency` | `s` | per-request time; percentiles via estimators | vLLM, MLPerf server scenarios |
| `time-to-first-token` | `s` | streaming first-response time | LLM inference |
| `peak-rss` | `By` | peak process resident set size | ASV `peakmem_`, pyperf, rustc-perf `max-rss` |
| `allocated-bytes` | `By` | bytes allocated per operation | JMH GC profiler, BenchmarkDotNet, Go `B/op` |
| `allocations` | `1` | allocation count per operation | Go `allocs/op` |
| `object-size` | `By` | size of a produced object | ASV `mem_` |
| `gpu-peak-memory` | `By` | peak device memory | torch `max_memory_allocated` |
| `instructions` | `1` | retired instructions; deterministic when simulated | rustc-perf default metric |
| `cycles` | `1` | CPU cycles | noisier than instructions |
| `estimated-cycles` | `1` | simulator-weighted cycles (synthetic) | CodSpeed, Cachegrind models |
| `cache-misses` | `1` | cache misses, level named if specific | perf, JMH perfnorm |
| `branch-misses` | `1` | branch mispredictions | perf, BenchmarkDotNet |
| `page-faults` | `1` | page faults | perf, rustc-perf |
| `throughput-bytes` | `By/s` | bytes processed per second | Google Benchmark, criterion |
| `throughput-items` | `1/s` | items/operations per second | ops/s, tokens/s, IOPS |
| `energy` | `J` | energy consumed | RAPL, Scaphandre |
| `power` | `W` | average power draw | SPECpower, mobile benchmarks |
| `cpu-frequency` | `MHz` | observed frequency; usually a condition measurement beside the measurand | pyperf metadata, cpufreq |
| `cpu-temperature` | `Cel` | sensor temperature; usually a condition measurement | pyperf metadata, hwmon, NVML |
| `load-average` | `1` | 1-minute load average before or after the attempt; a condition measurement | `/proc/loadavg`, pyperf |
| `throttle-events` | `1` | thermal or power throttle events across the attempt; a condition measurement | throttle counters, NVML |
| `binary-size` | `By` | linked artifact size; deterministic | rustc-perf `size:*`, LNT, Chromium |
| `build-time` | `s` | time to build the artifact | LNT, Bencher tutorials |
| `compression-ratio` | `1` | output/input size ratio | zstd, lzbench |
| `score` | `1` | quality metric paired with performance | LNT `score`, MLPerf accuracy |

## Appendix B: Recommended method, fact, and procedure keys (non-normative)

Method, environment facts, and procedure are open objects (§4.2). These keys are recommended spellings so that adapters agree. The method as executed goes under `method.protocol`; what it produced in the attempt under `procedure`; facts about the system under `environment.facts`; settings that change the artifact under test under `subject.configuration`.

| Key | Placement | Meaning | Example values |
|---|---|---|---|
| `protocol.name`, `protocol.version` | method | Declared measurement protocol identity | `benchmark-time` / `v1` |
| `protocol.timer` | method | Clock or event source for a time quantity | `perf_counter`, `process_time`, `cuda-events`, `cuda-synchronize`, `rdtsc` |
| `protocol.synchronization` | method | Device synchronization before reading a timer | `none`, `stream`, `device` |
| `protocol.warmup` | method | Warmup policy | `{"mode": "none"}`, `{"mode": "repetitions", "n_warmup": 3}`, `{"mode": "time", "seconds": 1}` |
| `protocol.calibration` | method | How inner iterations are chosen; an adaptive target may be a compound of several quantities | `{"mode": "adaptive", "minimum_sample_seconds": 0.01}`, `{"mode": "adaptive", "minimum_sample_seconds": 0.01, "target": "max(cpu-time, gpu-time)"}`, `{"mode": "fixed", "inner_iterations": 100}` |
| `protocol.repetitions` | method | How many observations the method takes. A harness that repeats at more than one level, as pyperf does with worker processes each producing several values, states the levels outermost first | `{"mode": "fixed", "n_repeat": 10}`, `{"mode": "adaptive", "max_seconds": 5}`, `{"mode": "fixed", "levels": [{"unit": "process", "n": 20}, {"unit": "value", "n": 3}]}` |
| `protocol.filesystem_cache` | method | Cache action the method performs | `unchanged`, `drop-before-attempt`, `drop-before-repetition` |
| `protocol.gc` | method | Garbage-collector policy during timing | `enabled`, `disabled`, `collect-before-repetition` |
| `protocol.probe` | method | Instrumentation for a non-time quantity, including condition measurements: sensor or counter and how it was read (`sampling.mode`: `interval`, `before-attempt`, `after-attempt`, `delta`) | `{"name": "OSSMemoryProbe", "counter": "rss", "sampling": {"mode": "interval", "seconds": 0.01}}`, `{"name": "hwmon", "sensor": "coretemp/package", "sampling": {"mode": "interval", "seconds": 1}}`, `{"name": "loadavg", "sampling": {"mode": "before-attempt"}}` |
| `protocol.threads` | method | A harness's own threading mode, when it is not settable per variant. Thread caps set through environment variables are **workload-variant parameters** instead (§4.2), so that one thread and ten can be compared within a suite | `{"mode": "harness-managed"}` |
| `protocol.schedule` | method | Interleaving the run followed: the unit alternated between sides, the ordering rule, and the number of rounds | `{"unit": "attempt", "rule": "alternating", "rounds": 5}`, `{"unit": "observation", "rule": "random", "rounds": 12}`, `{"unit": "pair", "rule": "random"}`, `{"unit": "attempt", "rule": "concurrent"}` |
| `configuration.execution_mode` | subject descriptor | JIT versus ahead-of-time execution of the subject | `jit`, `aot` |
| `cpus`, `numa_node` | resource selection | CPU or NUMA pinning applied to the measured process | `"0-7"`, `0` |
| `os.family`, `os.release`, `os.kernel` | environment facts | Operating system and kernel | `linux`, `ubuntu-24.04`, `6.8.0-31-generic` |
| `libc.name`, `libc.version` | environment facts | C library | `glibc`, `2.39` |
| `cpu.microcode`, `cpu.governor`, `cpu.boost`, `cpu.smt` | environment facts | CPU firmware and settings as read before the attempt | `0xa20120e`, `performance`, `false`, `true` |
| `gpu.driver`, `gpu.uuids`, `gpu.clocks_locked_mhz` | environment facts | Accelerator driver, device ids, and a clock lock if set | `550.90.07`, `["GPU-3f2a..."]`, `1500` |
| `container.image_digest` | environment facts | Image the attempt ran in | `sha256:4f1c...` |
| `runtime.python`, `runtime.implementation`, `runtime.compiler`, `runtime.cflags` | environment facts | Language runtime of the measured process | `3.12.4`, `cpython`, `GCC 13.2.0`, `-O3` |
| `process.environment`, `process.cgroup` | environment facts | Allowlisted environment variables and cgroup limits of the measured process | `{"OMP_NUM_THREADS": "1"}`, `{"cpu_max": "max"}` |
| `<collector>.*` | environment facts | Facts a context collector reported, under its name (`harness-adapter.md` §5.1) | `{"numpy-build": {"blas": "openblas-0.3.27"}}` |
| `inner_iterations` | procedure | Inner iterations actually used per observation | `100` |
| `attempted_repetitions`, `completed_repetitions` | procedure | Observations requested and obtained | `5`, `5` |
| `repetition_levels` | procedure | Per level, outermost first, repetitions requested and obtained, as totals over the attempt. For harnesses that repeat at several levels | `[{"unit": "process", "attempted": 20, "completed": 18}, {"unit": "value", "attempted": 60, "completed": 54}]` |
| `warmups_performed` | procedure | Warmup repetitions actually run | `1` |
| `caches_cleared` | procedure | Caches actually dropped, in order | `["filesystem"]`, `["filesystem", "cuda-jit"]` |
| `slot` | procedure | Position of this attempt in the run's realized order across all sides, zero-based | `0`, `3` |
| `round` | procedure | Position of this attempt in an interleaved run, monotone per run and kept by a retry; the same round number joins one baseline and one variant attempt | `0`, `1` |
| `duration_seconds` | procedure | Wall time spent on the attempt including warmup | `2.31` |
| `timeout_seconds` | procedure | Limit the attempt ran under, as ordered; an attempt stopped by it is `error` with reason `timeout` | `1800` |

`attempted_repetitions` and `completed_repetitions` are the totals at the innermost level. A harness that repeats at several levels also reports `procedure.repetition_levels`, outermost first, each `{unit, attempted, completed}` as totals over the attempt: twenty processes of three values attempted 20 processes and 60 values, and if two processes died the report is `process` 20 attempted and 18 completed, `value` 60 and 54. This keeps a lost process distinguishable from dropped values. Completed never exceeds attempted at any level.

A `pedantic` harness mode is `calibration.mode: fixed` plus `repetitions.mode: fixed`; an `adaptive` mode is `calibration.mode: adaptive` with the minimum sample duration it enforces. Changing any `protocol` value creates a new series under the default identity policy; changing a `procedure` value never does.
