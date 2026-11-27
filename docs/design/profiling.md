# Profile Capture

**Status:** Draft for review
**Companion to:** `benchmark-result-schema.md` §5.3 (external artifacts), `runner.md`, `system-decomposition.md`

## 1. Purpose

Measurements answer *did it change*; profiles answer *why*. This document
defines how profiles — CPU samples, allocation profiles, instruction-level
attribution, traces — are captured, attached to benchmark results, and
compared, without disturbing the measurements they explain.

The schema already gives profiles a home: an external artifact with media
type, URI, and checksum, referenced from a result. This document specifies
what surrounds that reference. Reviewed practice comes from ASV, rustc-perf,
CodSpeed, JMH, criterion, BenchmarkDotNet, Go/pprof, Firefox and Chromium perf
infrastructure, and the continuous-profiling systems (Google-Wide Profiling,
Parca, Pyroscope, Datadog, the OpenTelemetry profiles signal).

## 2. Design rules

1. **A profile is evidence attached to a result, never a measurement.**
   Profiles are artifacts referenced from results; they carry no estimate and
   join no series. The one crossover is instrumented measurement à la
   CodSpeed/Cachegrind: there the *counts* are ordinary (deterministic)
   quantities, and the profile remains an attached artifact.
2. **Profiled runs are separate runs.** Surveyed tools split three ways:
   a separate profile phase (criterion `--profile-time`, ASV, rustc-perf,
   Firefox re-trigger), profiling during measurement with overhead accepted
   (JMH, Go), or instrumentation-as-measurement (CodSpeed). The default here
   is the first: a profile capture produces **no observations**. The other two
   are permitted only declared — a deterministic instrument may measure and
   profile at once; a profile taken during measurement marks the result's
   procedure as perturbed so analyses can exclude it. Slowdown factors of
   3–20× under instrumenting profilers make silent mixing indefensible.
3. **Capture is requested, not implied.** The work order names which profile
   kinds to collect, with three triggering modes decided by project policy,
   not by the schema:
   - **on demand** — a person or the workbench asks;
   - **on finding** — a detected change or comparison verdict triggers a
     profiled re-run of the implicated revisions (the Firefox/Chromium model);
   - **always** — justified only when capture is cheap or deterministic
     (the CodSpeed model, rustc-perf's self-profile).
4. **One canonical interchange format, many raw formats.** The canonical
   archived form is a pprof profile (self-describing value types with units,
   well-defined merge and diff, build-id for symbolization); the emerging
   OpenTelemetry profiles format is its superset and the expected successor —
   adapters should treat "convert to pprof losslessly" as the goal. Native
   captures (perf.data, JFR, ETW/nettrace, measureme, pstats, Chrome trace)
   may be archived as-is when conversion loses information; folded stacks,
   flamegraph SVGs, and speedscope files are *renderings*, generated from the
   canonical form, not archived as sources of truth.
5. **A profile must be diffable against its counterpart.** Cross-revision
   comparison — the point of collecting profiles in a benchmarking system —
   requires: stable frame identity (symbolized names or build-id + address),
   identical value types and units, the sampling period recorded, and a
   normalization basis (iterations or operations performed) stored with the
   artifact so profiles of different durations compare fairly.

## 3. Profile artifact contract

Each profile artifact referenced from a result records:

| Field | Content |
|---|---|
| **kind** | what was profiled: cpu-samples, allocations, instructions, trace, … |
| **media type & schema** | format identification, per the existing artifact rule |
| **value types** | measured dimensions with units (e.g. cpu/ns, alloc/By) |
| **acquisition** | sampling period or instrumentation mode; capture duration; phase (separate profile run vs. during measurement) |
| **normalization basis** | iterations/operations the capture spans |
| **symbolization** | state (symbolized / needs build artifacts) and build-id(s) |
| **URI & checksum** | as already required for external artifacts |

The owning result supplies the rest of the identity — series, revision,
environment, run key — so profiles inherit exactly the comparison identity of
their benchmark, and "same series, two revisions" selects the pair to diff.

## 4. Component responsibilities

- **Work order** names the profile kinds and trigger; a profile request is a
  work order like any other and needs no scheduler.
- **Runner** executes the profile phase after (or instead of) measurement,
  captures the native output, and records acquisition metadata honestly.
- **Adapter** knows the harness/profiler invocation and converts native
  output to the canonical format where lossless.
- **Store** keeps the reference, checksum, and metadata; bulk profile bytes
  live in artifact storage, not in the result store.
- **Views** render flamegraphs and, centrally, **differential** views between
  two results of one series — subtraction (pprof `-diff_base`) for equal
  bases, share-of-total normalization when bases differ.
- **Detector / comparator** findings may carry "profile these two revisions"
  as a suggested follow-up work order.

## 5. Retention

Following universal practice, retention is tiered and is project policy:
profiles attached to ordinary results are droppable after a window; profiles
attached to change points, regressions, and releases are kept long-term. The
result's artifact reference outlives the bytes — a pruned profile leaves an
auditable stub (metadata and checksum), never a dangling mystery.

## 6. Format migration path

The system is designed for an eventual switch to OpenTelemetry profiles
without betting storage on it before it stabilizes. The asymmetry that makes
this safe: OTel profiles is a lossless superset of pprof, so pprof converts
up losslessly forever, while its own wire format (currently a
`v1development` proto) may still break.

- **Archive canonical: pprof**, the compatibility anchor.
- **Ingest accepts both**: an OTel profile is down-converted for the
  canonical copy; if that drops per-sample timestamps or links, the original
  is kept as a raw artifact — the same rule as for perf.data or JFR.
- **Capture may use OTel tooling now** (the eBPF profiler is just another
  capture mechanism adapters convert from).
- **OTLP export is an adapter concern**, symmetrical with the schema's
  stance on OTLP metrics (§6.3).

Three rules keep the switch cheap: (1) a single codec module is the only
code touching raw profile bytes — everything else works from the §3
contract; (2) media type is recorded per artifact, so both formats coexist
and legacy artifacts never need migration, only lossless up-conversion on
read; (3) core workflows depend on no pprof-only tooling behavior and no
OTel-only feature.

**Switch trigger, decided in advance:** when the OTel profiles proto leaves
`v1development`, collector support is generally available, and at least one
relevant backend ingests it natively, new captures become OTel-canonical and
pprof becomes the accepted legacy format at the same boundary.

## 7. Open questions

1. Is conversion-to-pprof an adapter duty at capture time or a store-side
   service on ingest?
2. Should "profile on finding" be automatic-by-default in fleet deployments,
   given it consumes exactly the scarce machine time the stories complain
   about?
