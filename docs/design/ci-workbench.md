# CI Workbench

**Status:** Draft for review
**Companion to:** `system-decomposition.md` §3.4–§3.5, `runner.md`, `benchmark-environments.md` §5.2–§5.4, `comparator.md`
**Author:** Rok Mihevc

## 1. Role

A **CI workbench** is an unattended workbench (`system-decomposition.md` §3.4)
that carries out one benchmark job on a CI-provisioned compute node and
delivers its results durably, even across crashes, timeouts, or preempted
instances.

It sits in the same architectural slot as a local workbench (`bx` or
`spin bench`), wrapping the target provider, runner, store, and comparator for
an unattended remote environment instead of an interactive terminal:

| Responsibility | Local workbench (`bx`, `spin`) | CI workbench (e.g. GitHub Actions + EC2) |
|---|---|---|
| **Trigger** | Developer at a terminal | Scheduler or CI event (cron, PR command, manual dispatch) |
| **Target prep** | Calls target provider (`prepare` / `describe`) | Calls target provider (`prepare`, typically with shared build cache) |
| **Loop above runner** | Single run or interactive comparison session | Time-budgeted, resumable unit loop (or PR comparison rounds) |
| **Work orders** | `requester.kind: "workbench"` | `requester.kind: "ci"` |
| **Delivery** | Local store (`store.parquet`) | Write-once raw staging (e.g. S3) → serialized idempotent compaction (`bx ingest`) |

Two boundaries govern this design (`system-decomposition.md` §5, `runner.md`
§7):

- **It is a workbench, not a new kind of runner.** The runner (`runner.md`) is
  already complete for remote and cloud nodes: it takes one resolved work order
  against a prebuilt target on the current host and emits schema results. There
  is no "EC2 runner" inside benchx — the same runner executes unchanged inside
  an EC2 instance or a dedicated host.
- **CI platforms and cloud provisioning remain outside benchx.** Launching VMs,
  registering CI runners, enforcing wall-clock caps, and managing cloud secrets
  belong to the CI platform and its cloud integration.

What belongs in benchx's design is the contract where CI meets benchx: the
**provisioning handoff** (L0/L1 into benchx), the **CI workbench lifecycle**,
**durable delivery and restart semantics**, the **trust boundary**, and **where
implementations live**.

## 2. Overview

```text
  Events (cron, PR, /bench)          L0/L1 (CI platform + cloud)
             │                              │
             ▼                              ▼  provisions & names node
      ┌─────────────┐               ┌───────────────┐
      │  Scheduler  │───triggers───▶│ CI workbench  │  (requester: ci)
      │ (or CI YAML)│               └───────┬───────┘
      └─────────────┘                       │ 1. declare machine / L0–L1 facts
                                            │ 2. call target provider (prepare)
                                            │ 3. emit work orders (unit / round)
                                            ▼
                                    ┌───────────────┐   raw staging   ┌───────┐
                                    │   Runner +    │───or ingest────▶│ Store │
                                    │   Adapters    │                 └───┬───┘
                                    └───────────────┘                     │
                                                                          ▼
                                                                    Comparator /
                                                                    Views / Alerts
```

Mapped onto the environment layers of `benchmark-environments.md` §2.1:

| Layer | Responsibility in CI | Owner |
|---|---|---|
| **L0 Provisioning** | Launch and terminate VM/host, assign logical machine name, enforce max-lifetime watchdog, attach least-privilege role | CI platform / provisioner (outside benchx) |
| **L1 Machine session** | Apply OS/kernel/governor tuning (or use a fixed image), declare L0/L1 facts | Provisioner / image |
| **L2 Software environment** | Check out revision(s), set up dependencies, build with cache via target provider (`prepare`) | Target provider (`system-decomposition.md` §3.4) |
| **L3 Process** | Emit work orders (`requester.kind: "ci"`), enforce launch settings on measured processes | CI workbench → Runner |
| **L4–L5 Measurement** | Run harness per unit/round, capture context, emit schema results | Runner + Harness adapters |
| **Delivery & publishing** | Upload raw unit archives incrementally; compact/ingest into store; derive views and verdicts | CI workbench → Store ingest / Comparator |

## 3. Provisioning handoff (L0/L1 to benchx)

A CI workbench is agnostic to how the compute node is provisioned or entered.
Three common deployment shapes sit behind the same handoff boundary:

| Shape | How a job reaches the node | Strengths | Tradeoffs |
|---|---|---|---|
| **Dedicated pull node** | A long-lived physical or tuned host polls a repo or queue (e.g. systemd timer) | Lowest noise floor (fixed physical host and L1 tuning); no CI runner agent or cloud token needed | Fixed hardware capacity; operator maintains OS, disk, and updates |
| **Ephemeral self-hosted CI runner** | A CI job launches a fresh cloud VM, registers a one-shot runner, executes the job, and terminates the VM | Pay-per-run; parallel bursts for backfill or PRs; clean state every run; native CI logs and UI | Higher host-to-host variance; requires a runner-registration token and leak-safe teardown |
| **Direct cloud job (no CI runner agent)** | A CI workflow invokes cloud APIs (e.g. `RunInstances` + user-data, SSM, or Batch) and lets the VM self-terminate | Pay-per-run and parallel; cleanest security (cloud OIDC only, no CI runner token); reliable VM self-termination | Worse CI UX unless logs/status are streamed or polled from cloud storage |

Persistent, always-on self-hosted CI runners on cloud VMs are generally a poor
fit for public repositories: they pay 24/7 for idle capacity, accumulate state
drift, carry security risks on pull requests, and lose their same-host noise
advantage whenever stopped and started on a new physical host.

Whichever shape is used, the provisioner must satisfy three handoff rules
before invoking the CI workbench:

1. **Stable logical machine identity.** Ephemeral cloud instances receive a
   fresh hostname on every boot, which would split every run into a new series.
   The provisioner must assign a stable logical machine name bound to one
   instance type / hardware class (by setting the hostname or declaring the
   runner name) and prefer single-CPU-model instance families
   (`benchmark-environments.md` §5.4).
2. **Declared L0/L1 facts.** Facts the runner cannot detect — instance type,
   tenancy, base image or AMI identifier, and tuning playbook version — are
   passed into the work order or declared via the project/node configuration
   channel before execution starts. Prefer keeping configuration consolidated
   in `benchx.conf.json` in the root directory (or passing a per-node
   `BENCHX_SETUP_FILE` / work-order declaration) rather than introducing a
   hidden `.benchx/` directory (`benchmark-environments.md` §3.2, §9 Q4).
3. **Leak-safe lifecycle.** For ephemeral instances, provisioning and teardown
   bracket the job (`start` and an `always` `stop` step), backed by an
   instance-side max-lifetime shutdown/self-termination guard and periodic
   cleanup of orphaned instances so a failed CI workflow cannot leak compute.

## 4. CI workbench lifecycle

A CI workbench runs in one of two modes, matching the comparator's two modes
(`comparator.md` §1):

### 4.1 Tracking mode (continuous history)

Measures one revision on one logical machine to extend per-series history:

1. **Resolve inputs:** pin the target commit SHA, logical machine name, suite
   filter, and wall-clock budget.
2. **Inspect prior progress (for restarts):** list already-recorded units for
   `(commit, machine)` so completed work is skipped (§5).
3. **Prepare target:** invoke the project's target provider (`prepare`) for the
   pinned commit, using any configured build cache (e.g. `sccache` in object
   storage). Record resolved software-environment facts (such as a lockfile or
   explicit package list) alongside L0/L1 declarations.
4. **Execute units:** for each remaining unit (such as one benchmark binary or
   one suite command) while within the wall-clock budget, author a resolved
   work order (`requester: {"kind": "ci", "name": "..."}`), run it through the
   runner (`--no-ingest` when staging raw outputs), and upload the unit's
   results immediately.
5. **Record attempt outcome:** write an attempt summary and log (including any
   provider build failure, unit crash, or budget cutoff) and trigger store
   compaction/publishing.

### 4.2 Comparison mode (PR or A/B verification)

Evaluates a contender revision against a baseline on the same machine within
one job (`comparator.md` §2.3, UC-03):

1. **Prepare both targets** on the same compute node through the target
   provider (`prepare` for baseline and contender).
2. **Run an interleaved session:** share one `run_key`, emit one work order per
   side and round (`round`, `slot`, `requester.kind: "ci"`), and alternate
   sides across rounds — the same session loop a local workbench uses
   (`system-decomposition.md` §3.4).
3. **Ingest and compare:** ingest the run's results into an ephemeral or
   project store, invoke the comparator in run mode, and hand the resulting
   comparison document to alerting (e.g. a PR comment).

### 4.3 Build failures vs. benchmark failures

Following `system-decomposition.md` §3.4 and `runner.md` §6:

- A **target-provider failure** (checkout, dependency setup, or compile error)
  happens before any work order exists. The CI workbench records it in the
  attempt metadata and log and exits; it does not synthesize runner results.
- A **benchmark case failure or timeout** inside a unit is handled by the
  runner and recorded as an explicit schema result (`status: error` or
  `timeout`). A unit that completed with some failing cases is a finished unit
  and is not retried.
- A **unit crash** (binary failed to start or produced no results) is recorded
  as a failed unit attempt so a restartable workbench knows whether to retry
  up to the attempt cap (§5).

## 5. Durable delivery, partial runs, and restarts

Long benchmark suites on ephemeral machines will hit job timeouts, spot
preemptions, or hanging binaries. Core `benchx` does not yet implement restarts
itself — resuming an interrupted run is orthogonal to CI and equally useful for
long local runs (such as multi-binary suites in the Arrow/`archery` use case),
and currently lives in the outer workbench layer. For CI workbenches, combining
unit-level restarts with durable remote staging ensures completed work is never
lost and restarts are safe:

1. **Unit-level incremental staging.** Each unit's raw result documents and
   work orders are uploaded to durable staging (such as object storage) as soon
   as the unit finishes, rather than buffered until the entire job completes.
   At most the single unit in flight is lost on crash or timeout.
2. **Append-only, write-once staging.** The compute node only adds objects:
   write-once unit archives (enforced by storage policy where available, e.g.
   conditional put) plus per-attempt metadata and logs. It never reads back,
   merges, or mutates the compacted store.
3. **Forward-only restarts (the ratchet).** Run status is derived from the
   staged objects for `(commit, machine)` — no separate queue database is
   required. Re-dispatching the same pair skips every unit that already has a
   staged archive, paying for setup and a cached build again but never
   re-measuring finished units. Concurrency control per `(commit, machine)`
   prevents concurrent duplicate attempts, with write-once storage as the
   backstop.
4. **Idempotent compaction and ingest.** A separate, serialized job (typically
   on a hosted CI runner) reads staged unit archives, runs `bx ingest` into the
   result store (e.g. one Parquet file per `(commit, machine)` or a store
   service), and updates derived views and alerts. Because store ingest is
   idempotent (`benchmark-result-schema.md` §5.4), partial runs can be
   published immediately and re-compacted safely whenever later attempts finish.

For small, fast suites where incremental staging is unnecessary, a CI workbench
may alternatively ingest directly into a local or remote store at job end; the
contract with the runner and store is identical.

## 6. Trust and security boundary

Benchmarking executes arbitrary code from the repository under test. Any CI
deployment that accepts pull requests or runs on a public repository must
enforce three boundaries:

1. **Trigger gating.** Automated tracking runs follow trusted branches (e.g.
   `main`). Pull-request benchmark runs must be gated by explicit authorization
   (for example, a comment command restricted to maintainers or collaborators),
   never triggered automatically from unreviewed fork PRs.
2. **Least-privilege credential split.**
   - *CI control plane / workflow role:* may launch and terminate instances
     scoped to the project's tags/network, read staging, and write to the
     compacted store and published views.
   - *Compute node role:* may only append to the raw staging prefix (and
     read/write a build cache such as `sccache`). It must **not** hold
     credentials to modify or delete compacted store history, push to git
     repositories, or administer CI runners.
3. **Secret hygiene.** The runner records only allowlisted environment
   variables (`benchmark-environments.md` §3.1) rather than dumping the full
   process environment, so CI tokens (e.g. `AWS_TOKEN`, GitHub tokens) are
   never swept into results. The current allowlist mechanism in `benchx` is
   still early and ad hoc (a small built-in default covering thread, device,
   and loader variables such as `OMP_NUM_THREADS`, plus adapter additions and
   `$BENCHX_ENV_ALLOWLIST`); resolving the tension between excluding secrets
   and recording execution-shaping variables calls for a documented,
   configurable "safe to record/echo" allowlist (e.g. in `benchx.conf.json`).
   Provisioning scripts and target providers must never place secrets in
   declared provenance (`how_built`, `activation`, or declared setup/config
   facts).

## 7. Packaging and repository boundaries

To keep the core `benchx` package small, cloud-free, and CI-vendor-neutral,
responsibilities split across three layers:

1. **Core `benchx` repository ([`benchx-dev/benchx`](https://github.com/benchx-dev/benchx)):**
   schemas, runner, adapters, local workbench (`bx`), store, comparator, and
   this design contract. Contains no cloud templates (CloudFormation/Terraform)
   or CI-platform workflows.
2. **Reusable CI workbench repository (reference implementation:
   [`benchx-dev/benchx-ci`](https://github.com/benchx-dev/benchx-ci)):**
   generic CI workflows, infrastructure templates (e.g. AWS CloudFormation
   setup), unit-budget ratchet, and staging compaction scripts parameterized by
   project configuration.
3. **Project benchmark repository (reference implementation:
   [`benchx-dev/benchx-arrow`](https://github.com/benchx-dev/benchx-arrow),
   live dashboard at <https://benchx-dev.github.io/benchx-arrow/#/>):**
   project-specific files only — `benchx.conf.json`, the project's target
   provider (`provider/`), machine catalog (`machines.json`), thin caller
   workflows, and project metadata.

## 8. Reference deployment mappings

The same contract supports minimal dedicated deployments and cloud-ephemeral
deployments without changing benchx itself:

| Abstract role | Ephemeral GitHub Actions + EC2 + S3 | Dedicated systemd node + Git |
|---|---|---|
| **Scheduler / trigger** | Scheduled & comment-dispatch workflows checking staged objects | Cron / systemd timer + file queue in repo |
| **Provisioner (L0/L1)** | `start`/`stop` jobs (ephemeral self-hosted runner or direct EC2 launch) + hostname & max-lifetime watchdog | Fixed physical host, tuned once by operator |
| **Target provider (L2)** | Commit-specific env setup + incremental build with S3 `sccache` | Reused worktree + local incremental build |
| **CI workbench** | Unit loop with wall-clock budget, `bx bench --no-ingest`, write-once S3 upload | Per-commit script calling `bx bench` |
| **Compaction / store** | Serialized hosted publish workflow running `bx ingest` into Parquet on S3 | Local `bx ingest` into Parquet committed to repo |
| **Views & alerts** | Derived tables + static site / PR comments | Derived tables + static site |

## 9. Boundaries

- **Not the scheduler:** does not decide which commits to benchmark, manage
  fleet priorities, or perform regression bisection (`system-decomposition.md`
  §3.5).
- **Not the runner:** does not execute harnesses or capture machine snapshots
  itself; it authors work orders and invokes the runner (`runner.md`).
- **Not the target provider:** does not check out or build software itself; it
  calls the project's target provider before issuing work orders.
- **Not the provisioner:** does not prescribe a specific CI platform or cloud
  provider; it defines the handoff contract any provisioner can satisfy.
- **Not the store:** stages raw results and triggers ingest; identity
  assignment, validation, and deduplication remain in the store.

## 10. Open questions

1. **Reusable CLI helpers vs. deployment scripts:** Should core `benchx` ship a
   thin cloud-agnostic helper for resumable unit execution (unit-budget loop,
   restarts, and attempt manifest), or should CI workbenches stay small
   deployment-side scripts calling `bx bench`, `bx run`, and `bx ingest`?
   *Current leaning:* start simple with deployment-side scripts and let real
   usage guide what (such as restart support, which is also useful locally) is
   worth moving into core `benchx`.
2. **Attempt and staging manifest schema vs. usage guides:** Should the staging
   layout and attempt record (units expected, units completed, provider
   failure log, status derivation) become a versioned benchx schema, or remain
   a convention documented in usage guides? *Current leaning:* start with
   usage guides for concrete setups; once at least two CI setups exist,
   extract what turns out to be genuinely reusable versus system-specific.
3. **Cross-revision environments in PR comparison mode:** When baseline and
   contender revisions require different dependency sets on an ephemeral
   instance, the setup takes the cross-environment shape of
   [UC-04](../use-cases/UC-04-cross-env.md) (cf. the
   [`pandas-asv`](https://github.com/benchx-dev/benchx_examples/tree/main/examples/pandas-asv)
   example, which prepares both environments up front and alternates `bx bench`
   rounds under a shared `--run-key`). What remains open for CI is how a CI
   workbench should budget the setup/build cost of two distinct environments
   against measurement rounds within a fixed job budget, and how to surface
   cases where code and dependencies vary together.
4. **Environment-variable allowlist:** How should `benchx` evolve its
   currently ad hoc allowlist (`benchmark-environments.md` §3.1) into a
   documented, configurable "safe to record/echo" list (e.g. in
   `benchx.conf.json`) that keeps secrets such as `AWS_TOKEN` out of results
   while reliably capturing execution-shaping variables such as
   `OMP_NUM_THREADS`?
