# Runner Architecture

**Status:** Draft for review
**Companion to:** `system-decomposition.md` §3.3, `benchmark-result-schema.md`, `benchmark-environments.md`

## 1. Role

The runner executes benchmark work on one compute node and reports what
happened as schema results. It is the only component that touches real
hardware. It runs a target that was built elsewhere and never builds. It does
not decide what to run, store history, or judge outcomes.

One runner design serves both deployment shapes: invoked by the scheduler on a
fleet machine, or by a person (via the workbench) on a laptop. The pipeline is
identical; only precision settings and environment strictness differ.

## 2. Principles

1. **A run is described before it is executed.** Every run starts from an
   explicit, self-contained **work order**; nothing is inferred from leftovers
   on the machine. What the work order doesn't specify, the runner records.
2. **Honest identity or no result.** A result that cannot state its source
   revision, build configuration, and machine identity is a defect. Dirty
   working trees, unknown builds, and stale binaries are detected and marked,
   never silently measured. Build configuration is read from the prebuilt
   target or declared in the work order; the runner does not choose it.
3. **Control the process, record the machine.** Settings of the process the
   runner launches (thread caps, pinning, device selection, environment
   variables) it enforces and records. Conditions it does not control (kernel,
   governor, load, temperature) it snapshots as observed context, and it
   checks the ones the work order requires (hardware present, quiescence).
4. **The harness measures; the runner surrounds.** Measurement itself belongs
   to the native harness and its adapter. The runner prepares, invokes,
   captures, and reports.
5. **Every outcome is a result.** Harness crash, timeout, unmet policy, or
   explicit skip each produce a schema result with the corresponding status —
   absence of a result always means "was not attempted."
6. **Two interfaces, both documents.** The runner touches the rest of the
   system only through two contract artifacts: work orders in, results out.
   No component is required at either end — a scheduler, a CI job, or a
   person may author work orders; results may go to a live store, a local
   file, or both. The runner never knows or cares which.

## 3. The work order

The runner's single input. It names:

- **what** — benchmark suite(s) and case filters, and the quantities to collect;
- **at what** — the target, which is always prebuilt: an existing build
  directory, a working tree, or an artifact;
- **how** — harness precision settings (repetitions, minimum time), the
  environment policy to apply, and any declared build configuration;
- **for whom** — provenance to thread through: run key, requester, reason.

A work order is a **versioned, self-contained, replayable contract
artifact** — the same design discipline as the result. Results reference the
work order that produced them in provenance, so any run is reproducible from
its stored order. Work orders come from a scheduler, a CI job, the workbench,
or a shell; the runner does not care which.

## 4. Run pipeline

Each work order flows through five stages:

```
identify target → prepare environment → execute → capture context → emit results
```

1. **Identify target.** Locate the code to measure and establish what it is.
   The target is always prebuilt: verify an existing build directory
   (recording its configuration and detecting staleness), inspect a working
   tree, or accept an artifact. The runner never checks out a revision to
   build it and never invokes a build. Whoever prepared the target owns
   building, caching, and reusing it.
2. **Prepare environment.** Apply the environment policy to the process the
   runner will launch: thread and affinity caps, accelerator selection,
   environment variables; then verify what the policy demands of the node:
   hardware present, quiescence. Policies are per-project and per-node; a
   laptop policy checks little, a tuned bare-metal policy checks much. The
   runner changes nothing outside the launched process. Anything the policy
   demands, such as requested hardware, that the node cannot deliver fails the
   run visibly.
3. **Execute.** Invoke the native harness through its adapter, scoped by the
   work-order filters, with the requested precision.
4. **Capture context.** Assemble identity and conditions: machine and
   environment identity, build configuration, source revision (including
   dirty state), and the observed-context snapshot (kernel, versions,
   frequency governor, load, temperature where available).
5. **Emit results.** Combine adapter output with captured context into schema
   results — one per case × quantity, including failures and skips — and
   deliver them: to the store's ingest API, to a local result file, or both.

Stages are separable: "identify target" without "execute" is a dry run that
reports what would be measured; a saved result file replayed as a comparison
baseline skips straight to emit.

## 5. Environment policy

The named, versioned set of demands a run must satisfy, referenced from the
work order:

- what to **enforce**, on the launched process only: thread caps, CPU pinning,
  device selection, synchronization discipline for accelerators;
- what to **verify**: required hardware present, quiescence, clean tree, and
  machine state the runner cannot set (governor, boost, SMT), with `refuse` or
  `warn` per rule;
- what to **record**: everything enforced and verified, plus the
  observed-context snapshot.

The policy's identity-relevant parts land in the result's comparison context
and environment identity, so "measured under policy X" is visible and
comparable. Loosening or tightening a policy is therefore a series-splitting
event, not a silent change.

## 6. Failure handling

- Harness error, timeout: an `error` result carrying logs in provenance.
- Target not identifiable (build directory missing, artifact checksum
  mismatch): the run is refused before execution, with the reason reported.
- Partial suite completion: per-case results for what ran, `error`/`skipped`
  for what did not — never a truncated table.
- Environment policy unsatisfiable: the run is refused before execution, with
  the reason reported to whoever ordered it.
- The runner is stateless between work orders; a crashed runner loses at most
  the in-flight run.

## 7. Boundaries

- **Not the scheduler:** takes orders, never creates them.
- **Not the adapter:** harness knowledge lives in adapters the runner
  invokes. An adapter has two halves: **driving** (translating work-order
  scoping and precision into the harness's own invocation) and
  **translating** (native output plus supplied context into schema results).
  The translating half stands alone, so importers and CI users can apply the
  same mapping without a runner.
- **Not the comparator:** emits results, never verdicts.
- **Not a builder:** runs targets it is given; checking out, configuring,
  compiling, installing, and caching builds belong to the project's target
  provider (`spin`, `archery`, CMake, a CI script), which the orchestrator (the
  workbench, a CI job, or the scheduler) calls before it issues the order
  (`system-decomposition.md` §3.4).
- **Not a provisioner:** runs on machines it is given; fleet membership,
  machine lifecycle, and machine tuning (governor, boost, SMT, clock locks)
  belong to operations and the scheduler. It verifies and records that state;
  it does not set it.
- **Not a queue:** by explicit decision, the runner holds no intake queue —
  it executes one work order per invocation, and buffering, ordering, and
  serializing pending work belong to whoever submits it (scheduler, CI, or
  person).

## 8. Open questions

1. How much of the environment policy vocabulary is core versus per-project
   plugin (e.g. accelerator synchronization differs per backend)?
2. Is "verify an existing build directory" fully solvable, or do we accept
   best-effort staleness detection with prominent marking?
