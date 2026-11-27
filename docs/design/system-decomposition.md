# System Decomposition

## 1. Purpose

This document maps the components of BenchX and defines what each one does —
functionally, not technologically.

Two rules shape the decomposition:

1. **The schema is the only shared language.** Components exchange benchmark
   results, work orders, and comparisons as the [schemas](../schemas/index.md)
   define them, never internal state.
2. **Components are separable.** A project can adopt one piece — just the local
   CLI, or just the store and views — without the rest.

## 2. Overview

```text
                        produce results               consume results
  ┌─────────────────┐  ┌────────────┐   ┌───────┐   ┌────────────┐  ┌──────────┐
  │ Local workbench │─▶│            │   │       │   │ Comparator │  │  Views   │
  │      (bx)       │  │  Runner +  │──▶│ Store │──▶│(bx compare)│  │(bx view) │
  └─────────────────┘  │  Adapters  │   │       │   └────────────┘  └──────────┘
  ┌─────────────────┐  │            │   └───▲───┘
  │  CI workbench   │─▶│            │       │         Planned extensions:
  │   (benchx-ci)   │  └────────────┘       │         Scheduler · Detector
  └────────▲────────┘  ┌───────────┐        │         Alerts · Importers
           │           │ Importers │────────┘
    Target provider    └───────────┘
  (project-owned build)
```

Everything to the left of the store produces schema results; everything to the
right only reads them.

| Component | Role | Status | Details |
| --- | --- | --- | --- |
| **Result store** | Idempotent record of raw results and series identity | Shipped (`store.jsonl`, `store.parquet`) | [Result schema](benchmark-result-schema.md), [Parquet](measurement-result-parquet.md) |
| **Harness adapters** | Drive a native harness and translate its output | Shipped (`pyperf`, `google-benchmark`, `asv`) | [Harness adapters](harness-adapter.md) |
| **Runner** | Execute one work order on one node | Shipped | [Runner](runner.md) |
| **Workbench** | Orchestrate runs, comparison rounds, and targets | Shipped (`bx`, [`benchx-ci`](https://github.com/benchx-dev/benchx-ci)) | [CLI guide](../benchx.md), [CI workbench](ci-workbench.md) |
| **Target provider** | Project-owned hook that builds or describes a target | Extension point | [Target description](../schemas/target-description.md) |
| **Comparator** | Decide whether performance changed between A and B | Shipped (`bx compare`) | [Comparator](comparator.md) |
| **Views** | Static HTML/JS dashboard over stores or CI runs | Shipped (`bx view`) | [CLI guide](../benchx.md) |
| **Scheduler, Detector, Alerts, Importers** | Fleet queuing, history scanning, PR alerts, migrations | Planned | [§4 Planned extensions](#4-planned-extensions) |

## 3. Components

### 3.1 Result store

**Role:** The system of record for benchmark results (`store.jsonl` by default,
or `store.parquet` with `benchx[parquet]`).

- Accepts results in the schema's ingest form; validates, checks unit
  consistency, and stores them idempotently with full provenance.
- Maintains series identity: assigns each result to its series via the
  [identity policy](../schemas/identity-policy.md), computes fingerprints, and
  guards immutability of identity.
- Answers queries: find a series, order its results, retrieve observations and
  provenance, and inspect recent runs.
- Keeps raw observations so better statistics can be applied to old data later.

**Does not:** run anything, compare anything, decide anything. It stores and
serves facts.

### 3.2 Harness adapters

**Role:** Drive one native harness and translate its output into
[measurement results](../schemas/measurement-result.md).

- One adapter per harness (`pyperf`, `google-benchmark`, `asv`, …), following
  the schema's mapping rules: samples become observations, aggregate statistics
  become typed summaries, and harness metadata splits into workload parameters,
  protocol, environment, and provenance.
- Separates counters that describe the workload (case parameters) from those
  that describe the measurement (procedure).
- Emits one result per quantity: a run that measures wall time, GPU time, and
  peak memory produces one result in each of three series.
- Preserves failure semantics: a failed, skipped, or partially failed
  benchmark becomes an explicit result status, never a silent absence.

**Does not:** judge results or invent metadata the harness did not report.

### 3.3 Runner

**Role:** Execute benchmark work on one compute node and report what happened.

- Takes a [work order](../schemas/work-order.md) and carries it out: runs
  against a prepared target, which it never builds, invokes the harness, and
  collects output through the adapter.
- Applies process-level settings requested by the order (environment variables,
  timeouts), refuses unsupported orders, and records host and environment facts
  so no setting is lost from results.
- Captures identity honestly: machine, build configuration, compiler, source
  revision, dirty working tree. A result that cannot state where it came from
  is a defect.
- Behaves identically whether invoked by a person on a laptop or by CI on a
  fleet machine.

**Does not:** decide what to run or when, store history, or compare results.

### 3.4 Workbench

**Role:** The orchestrator above the runner and comparator for a given
execution context — either interactive local use on a developer's machine
(**local workbench**) or unattended execution on a CI-provisioned node
(**CI workbench**).

A **local workbench** (`bx`):

- Runs (`bx bench`) and compares (`bx compare`) benchmarks on the developer's
  own machine, offline: current workspace against a baseline build, one build
  configuration against another, or a checkout against a release tag.
- Obtains targets through the project's **target provider** (below) and drives
  the session loop for a comparison: one work order per side per round,
  alternating the sides, then the comparator.
- Saves every run as self-describing results that can be inspected, replayed, or
  promoted into a shared store unchanged.
- Allows dirty working trees locally (marked dirty and labeled local-only in
  comparisons) and refuses invalid or unidentifiable comparisons up front.

A **CI workbench** ([`benchx-ci`](https://github.com/benchx-dev/benchx-ci),
[ci-workbench.md](ci-workbench.md)) plays the same orchestrating role on
CI-provisioned nodes — adding logical machine identity, time budgets, resumable
execution, and durable result upload — while invoking the same runner unchanged.

**Does not:** build anything itself or provision cloud machines.

**Target provider (extension point, not a component).** The runner never
builds, so a project-owned tool (`spin` for NumPy, CMake/Ninja for Arrow, or a
custom script) turns a source ref such as `main` or `WORKSPACE` into a prepared
target before any work order exists. Build failures happen before any work
order exists and stop the workbench immediately; no order or result is created.

A [target description](../schemas/target-description.md) reaches the workbench
by three routes:

1. **A provider command** in `benchx.conf.json`, reading a
   [target-provider-request](../schemas/target-provider-request.md) (`prepare`
   or `describe`) on stdin and printing a target description on stdout.
2. **A sidecar file** (`.benchx-target.json`) in a long-lived build directory.
3. **Direct CLI input** when the user prepared everything by hand.

Optional provenance fields (`how_built`, `activation`, `shell`) are recorded
verbatim for human readers and are never executed by BenchX.

### 3.5 Comparator

**Role:** Answer "did performance change between A and B?" defensibly.

- Compares two sets of results — contender against baseline — and classifies
  each shared series as improved, regressed, unchanged, or indeterminate, with
  the evidence for the verdict in a
  [comparison document](../schemas/comparison-document.md).
- Uses the observations and dispersion the results carry, so the threshold for
  "real change" reflects each benchmark's own noise rather than one flat
  percentage.
- Enforces comparability before judging: mismatched identities or incompatible
  units are reported, not silently dropped from the table.
- Serves every scale of question with the same semantics: two local builds,
  one PR against `main`, or one release against another.

**Does not:** watch history over time (that is the change detector) or decide
whether a regression is acceptable (that is project policy).

### 3.6 Views

**Role:** Make the store's contents visible without a backend server or custom
frontend work.

- Builds a self-contained static HTML/JS dashboard (`bx view`) from a local
  store, result files, or CI runs directory.
- Shows commit summaries, suite tables, parameter-filtered series history
  charts, and failure details.
- Supports JSON view payloads by default and byte-range-sliced Parquet payloads
  (`--views-format parquet`) for large multi-machine CI histories such as
  [arrow.benchx.dev](https://arrow.benchx.dev/).

**Does not:** own any data or compute any verdicts; every number shown is
traceable to stored results.

## 4. Planned extensions

These roles fit around the store without changing the core pipeline, and are not
yet part of the core package:

- **Scheduler:** Queueing, hardware routing, and automated culprit bisection for
  shared fleets. Optional — GitHub Actions workflows or scripts can author work
  orders directly.
- **Change detector:** Continuous scanning over stored series to flag step
  changes and slow drifts calibrated to each series' own noise.
- **Alerting and annotations:** Delivering findings to PRs or issues and
  recording human decisions (*accepted regression*, *noisy machine*) so alerts
  do not fire forever.
- **Importers:** One-time historical migrations from existing systems (Conbench,
  ASV, Codespeed, LNT) that preserve provenance and explicit gaps.

## 5. Boundaries of the system

Not components of this system:

- **Benchmark suites themselves.** Projects own their benchmarks and write
  them in their language's native harness; the system meets them at the
  adapter.
- **CI platforms and machine provisioning.** BenchX integrates with them; it
  does not replace them.
- **Decision policy.** What counts as an acceptable regression is a project
  decision — never baked into results or identity.
