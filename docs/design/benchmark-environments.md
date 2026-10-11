# Benchmark Environments

**Status:** Draft for review

**Companion to:** `benchmark-result-schema.md` §4.2 (field placement; where this document and the schema differ, the schema wins), `harness-adapter.md` §3 (the context document), `runner.md`, [`docs/user_stories/`](../user_stories/), [`docs/use-cases/`](../use-cases/)

**Author:** Rok Mihevc

## 1. Purpose

A benchmark environment is everything a benchmark runs on and in: the machine and its tuning, the operating system, the libraries and the build under test, and the process settings the harness inherits. This document says who is responsible for that environment, what benchx does with it, and what we recommend to the people who set it up.

The short version: **the user prepares the machine and the software, and benchx runs a prebuilt target in it, controls the process it launches, and records everything.** benchx is a utility for using a benchmark harness more productively. Its work starts where the runner identifies a target that already exists and ends when results are emitted. It does not provision, tune the machine, build, or install. What it does control is the process it launches: thread caps, CPU affinity, device selection, and the environment variables set on the measured process. It checks that what a work order asks for is present, and it refuses the run when it is not.

The rest of the document has three parts. §2 and §3 are normative: the boundary, and what benchx records. §4 to §7 are recommendations, addressed to whoever prepares an environment, from a contributor's laptop to a dedicated fleet node. They are advice, not requirements; benchx works the same whether or not they are followed, and the only difference is how much the results can be trusted and how well they describe themselves.

## 2. The boundary

### 2.1 Layers of an environment

| Layer | Examples | Who sets it up | What benchx does |
|---|---|---|---|
| **L0 Provisioning** | hardware, instance type, firmware settings, SMT, isolated cores, kernel | operator, at install or boot time | records what it can detect; records what is declared |
| **L1 Machine session** | frequency governor, turbo, IRQ affinity, stopped services, a quiet machine | operator or user, usually privileged | records what it can detect; records what is declared |
| **L2 Software environment** | virtualenv or conda environment, build directory, BLAS, dependency versions, container image | user, with their own tools (`spin`, `archery`, `uv`, `pixi`, CMake, a CI script) | **starts here**: runs against the prebuilt target, builds and installs none of it, records it |
| **L3 Process** | `OMP_NUM_THREADS`, `taskset`, `numactl`, `CUDA_VISIBLE_DEVICES` | the work order, which the runner applies to the process it launches; or the user, in the shell or CI step that calls benchx | applies what the work order requests, passes through what it does not, records what the harness ran under |
| **L4 Workload variant** | JIT and kernel caches, filesystem cache, per-variant device setup | the harness and the benchmark author | records what the harness reports |
| **L5 In-harness** | timer, device synchronization, GC, warmup, calibration, interleaving | the harness | records what the harness reports and what the adapter's driving half requested |

### 2.2 Principles

1. **The user prepares the target and the machine; benchx controls only the process it launches.** Everything from L0 to L2 exists before benchx is invoked, and is provided with the user's own tools. benchx never builds or installs, and changes no setting of the machine or of the software environment. At L3 it applies what the work order requests (thread caps, affinity, device selection, environment variables) to the measured process alone, so nothing needs restoring afterwards.
2. **Verify what was asked for; record the rest.** The environment policy a work order names has three tiers: `enforce` (applied to the launched process), `verify` (checked, and the run refused or warned per rule), and `record` (observed only). A run that asks for hardware the node lacks, or for a condition the runner cannot deliver, is refused visibly. State the order did not ask about, such as an untuned machine, a dirty tree, or a declared fact that contradicts a detected one, is recorded as it is and never gates the run. Judging whether two results are comparable belongs to the comparator's eligibility guard (`comparator.md` §3) and to project policy.
3. **No extra steps.** benchx adds nothing around the harness beyond the launch settings above: no build, no setup, no cache clearing, no teardown, no cleanup. When a harness cleans up after itself, that is the harness's behavior.
4. **The harness owns the measurement loop.** Warmup, calibration, repetition, and interleaving of sides are harness features. benchx does not wrap a loop of its own around the harness.
5. **Declared and detected.** A fact reaches a result because the user declared it or because benchx or a plugin detected it. Both are recorded when both exist, each with its source (§3.4).
6. **Tell environments apart, do not reproduce them.** The record is detailed enough to see that two environments differed and in what (schema §4.2). Rebuilding an environment from a result is not a goal.

### 2.3 What this rules out

benchx has no notion of a revision to check out and build, no build cache, no privileged helper that changes machine state (governor, boost, SMT, clock locks), no sandboxing of untrusted code, and no restore-after-run. A work order never names a revision to build, only a target that already exists. The tools that already do these things well keep doing them, and call benchx last.

This moves some current behavior out of benchx's scope rather than dropping it. `archery benchmark diff WORKSPACE <tag>` clones and builds a revision itself (Arrow local story); in this design that build stays in `archery`, or `spin`, or a CI script, called as the project's target provider by the workbench or CI job (`system-decomposition.md` §3.4), and benchx is handed the two resulting build directories. What benchx adds is that the two directories now describe themselves in the results, which is the pain that story actually reports.

Enforcement is not ruled out; it is limited to the launched process. The runner enforces thread caps, affinity, device selection, and accelerator synchronization discipline on the process it starts, and verifies that the node can satisfy what the order requests. Conditions that would need privileged, persistent changes to the machine are `verify` or `record` rules only.

## 3. What benchx records

Environment facts reach a result through the adapter's context document (`harness-adapter.md` §3). This section defines where those facts come from.

### 3.1 Zero-configuration snapshot

With no configuration, benchx detects the following where the platform makes them readable without privileges:

| Group | Facts |
|---|---|
| **Host** | host name, CPU model, core and hardware-thread counts, memory, architecture, visible accelerators and their models |
| **System** | OS and version, kernel, libc, frequency governor, turbo and SMT state, load average, temperature |
| **Process** | CPU affinity mask, cgroup CPU and memory limits, and an allowlist of environment variables that shape execution: the thread and device variables (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `CUDA_VISIBLE_DEVICES`, and similar) and the dynamic loader's search variables (`LD_LIBRARY_PATH`, `DYLD_LIBRARY_PATH`, `LD_PRELOAD`) |
| **Runtime** | interpreter or VM and its version; installed package versions as a dependency manifest artifact |
| **Source** | revision, dirty flags, and working-tree ids of the subject and benchmark checkouts (schema §4.1) |

Three rules apply. A value that cannot be read is absent, never a placeholder. The process environment is never dumped wholesale, because CI environments carry secrets; only allowlisted variables are recorded, and a project can extend the allowlist. The default list names no build or environment management tool, so variables such as a virtual-environment or conda prefix are for the project to add; `how_built` and `activation` (§3.2) say how the environment was entered, whichever tool was used. And because the harness runs as a child of the runner, the process-level facts the runner reads from itself, after applying any launch settings from the work order, are the ones the harness actually ran under. Where the order applied a setting, the applied value is recorded as enforced; where it did not, the inherited value is recorded as observed.

### 3.2 Declared facts

What cannot be detected is declared by the user: how a dependency was installed and built, which BLAS is linked, the build configuration of a directory benchx was pointed at, the playbook or image version a node was prepared with, an operator-assigned runner name, a cloud instance type. Declarations travel in the work order or project configuration and are recorded as given. They are not checked against anything. Two declarations come with every prepared target (`runner-schema.md` §3.1): `how_built`, what the user did to build it, and `activation`, what the user ran to enter its environment, with the `shell` it is written for. They are free text, recorded in `provenance.info` and never executed. The runner records the environment it inherited next to `activation`; when the two disagree, both stay.

### 3.3 Plugins

Anything recorded beyond §3.1 comes from plugins: read-only describers that know one build system, package manager, or accelerator stack and return facts, for instance by reading a `CMakeCache.txt`, asking `numpy.show_config()`, or querying a GPU driver. A plugin never changes the environment. The plugin interface and catalog are out of scope for this document.

### 3.4 Where facts land

Placement follows the schema (§4.2); nothing new is introduced:

| Fact | Schema destination |
|---|---|
| runner name, CPU model, cores, accelerators, memory, instance type | `coordinates.environment` identity and metadata |
| the core set, GPU, or partition one result used | `coordinates.resource_selection` |
| build type, compiler and flags, install method, BLAS backend, JIT or AOT | `coordinates.subject.configuration` |
| dependency versions that policy treats as identity | pinned components of `coordinates.subject` |
| thread caps, affinity, host runtime version | `coordinates.comparison_context` (`protocol.threads`, `protocol.affinity`) |
| kernel, libc, driver, microcode, governor, turbo, load, temperature, image digest | `observed_context` |
| full package inventory | dependency manifest artifact in `provenance` |
| preparation playbook or image version | declared; `environment.metadata` by default, identity if the project's identity policy says so |
| `how_built`, `activation`, `shell`, target provider | declared; `provenance.info`, never identity |

Declared and detected map onto the schema's existing split between intended and realized. A declared setting is an intended value and lands in comparison context or the subject descriptor. The matching detected value is the realized one and lands in procedure or observed context. When a setting is detectable but nobody declared an intent, as with a thread cap inherited from the shell, the realized value is recorded as the intended one too. When declared and detected disagree, both are kept where this rule puts them; benchx picks no winner, and a comparator can see the disagreement.

Every environment fact carries its source: `declared`, `detected`, or the name of the plugin that reported it. How the source is represented in the message is an open question (§9).

Which conditions are identity and which are annotation is project policy, not a property of the condition (schema §1). The default above treats kernel and libc as observed context; a project benchmarking `malloc` promotes libc into identity through its identity policy, and producers do not move fields on their own.

## 4. Recommendations for every environment

1. **Hold everything fixed except the thing under test.** The use cases each vary one coordinate: parameters (UC-01, UC-02), revision (UC-03), one dependency or build option (UC-04). An environment difference that is not the one under test is a confound, whether or not anyone notices it.
2. **Measure both sides of a comparison in the same environment, close together in time.** Same machine, same session, same shell. A baseline measured last month on another machine is a different experiment (Arrow local story, "Reuse a measurement"). benchx records enough for the comparator to see the difference, but the cheapest fix is not to create it.
3. **Set thread counts explicitly.** Export `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and their relatives rather than inheriting whatever the machine defaults to, and avoid oversubscription when the harness or the subject runs its own thread pool (Array API story). Request them in the work order, or set them in the script that calls benchx, so that they are the same every time; benchx records what the harness ran under either way.
4. **Pin what you compare across.** Use a lockfile or an explicit version for every dependency whose performance matters, and say how it was installed. A PyPI wheel and a local source build of the same version are different artifacts with different performance, and the version alone does not distinguish them.
5. **Declare what benchx cannot see.** Build options behind a build directory, the BLAS vendor, the meaning of an environment label. The Arrow local story's first pain is a comparison of two opaque directory paths; a declaration is what turns `/tmp/bench-hardened` into "hardening on".
6. **Give environments stable names.** A runner name assigned by a person survives reinstallation and cloud host-name churn. A name derived from the host does not.
7. **Change one thing at a time, and leave a trace.** When the environment itself must change, such as an OS upgrade or a new compiler, change only that, and mark when it happened (§6).
8. **Prefer measurements that the environment cannot disturb, where they answer the question.** Deterministic quantities such as instruction counts, allocation counts, or binary size (schema §4.4) need no quiet machine. They do not replace wall time, and they do not see effects that live in the hardware, which is why OpenBLAS gets no signal from simulated counts, but they are the right tool on shared runners.
9. **Let the harness interleave.** When a harness can alternate sides or randomize order within one invocation, use that rather than running side A to completion and then side B. Interleaving turns slow drift, such as thermal state or background load, into noise that affects both sides equally.

## 5. Recommendations by kind of environment

### 5.1 Developer laptop or workstation

This is where most benchmarking in the collected stories happens (NumPy, one-off, Arrow local, Narwhals), and the governing fact is from the Arrow local story: on unreserved hardware the noise floor is a property of the moment, not of the machine.

- Plug in. On battery, and in any power-saving mode, frequency scaling dominates small effects.
- Close what you can: browsers, indexing, backups, video calls, and sync clients are the usual sources of bursts.
- Watch thermals. A laptop that has been compiling for ten minutes measures slower than the same laptop at rest. Let it settle after a build, and distrust a trend across a long run.
- Rely on within-run comparison. Measure baseline and contender in the same invocation or back to back, interleaved where the harness supports it, and do not compare against numbers from another day.
- Prefer more repetitions of a short, filtered run to one pass over a whole suite. A filter is a requirement at the scale of Arrow's 3,500 benchmarks or pandas' seven-hour suite.
- Keep long-lived build directories honest. They are necessary, since building both sides from scratch takes hours, and they drift. Rebuild the target before measuring, and remember that the recorded working-tree id, not the directory name, says what was measured.
- Do not tune the machine on benchx's account. Privileged tuning on a personal machine is rarely worth it and is easy to forget to undo. If you do tune, for instance with `pyperf system tune`, that is your step to run and to revert, and worth declaring.
- On macOS and Windows much of L1 is neither controllable nor readable. Expect a thinner snapshot and a wider noise floor, and lean harder on interleaving. On machines with performance and efficiency cores, the scheduler's choice of core is a further source of variance outside your control.

Results from such machines are valid and worth keeping. They are local evidence for a change under development, not points in a project's tracked history.

### 5.2 Hosted CI runners

Shared runners such as GitHub-hosted ones draw each job from a heterogeneous pool on shared hosts. The `github-action-benchmark` README warns of 10 to 20 percent variation between runs, and Nyrkiö's measurements (see `existing-components.md`) attribute a large share of apparent regressions on such runners to the infrastructure.

- Do not build an absolute wall-time history on them. Two consecutive jobs may not even run on the same CPU model.
- Do build relative measurements within one job: both sides of a comparison built and measured on the same runner in the same job, interleaved by the harness. UC-02's overhead ratio and UC-03's PR-against-main comparison both fit this shape.
- Use deterministic quantities for per-commit tracking when the project's performance is algorithmic rather than hardware-specific.
- Declare the runner class (for example `github-hosted/ubuntu-24.04`) as the environment's runner identity, and let the detected CPU model be recorded beside it. This keeps "hosted" visible as an identity rather than as a caveat, and lets a project decide whether jobs that landed on different CPU models share a series.
- Use them freely for what they are good at: checking that benchmarks still run, as pandas does on every commit.

### 5.3 Dedicated fleet node

A machine reserved for benchmarking, as in Apache Arrow's Conbench deployment, is the only environment in which small changes in wall time can be tracked across months. benchx prescribes none of what follows; it is what the project's provisioning and CI scripts should take care of before they call benchx. It draws on the experience of operating Conbench for Arrow, as collected in the Arrow user story, and on common practice for low-noise machines.

**Identity and history**

- Assign each node a stable name and declare it. Conbench keys history to the machine, and the Arrow story reports both failure modes that follow from getting identity wrong: a change that silently splits history, and, worse, one that silently blends incomparable results into one chart.
- Decide deliberately what is machine identity. If OS, kernel, or driver versions are identity, every routine upgrade fragments history. If they are recorded nowhere, upgrades are invisible. The schema's answer is to keep them in observed context, so they annotate a series without splitting it, and to treat a real hardware succession as a continuity event.
- A replacement machine is a new environment even under the old name. Run old and new side by side on the same commits for a while before retiring the old one, so the offset between them is measured rather than guessed.

**Quiet by construction**

- One benchmark job at a time per node. Serialize in the CI system or a queue in front of benchx; benchx holds no queue. Nothing else is scheduled on the machine.
- Disable unattended upgrades, periodic timers, indexing, and any monitoring agent heavier than a heartbeat. Apply upgrades deliberately, between runs.
- On Linux, the usual tuning applies: a fixed frequency governor (`performance`); turbo either off, or accepted as a source of variance; SMT off, or benchmarks pinned to one hardware thread per core; cores isolated from the scheduler and from interrupts for the benchmark's use; a fixed transparent-huge-page setting; NUMA placement pinned on multi-socket machines. `pyperf system` is a good checklist and reference implementation. For address-space randomization either choice is defensible, randomized layouts averaged over processes or a fixed layout, as long as it stays the same.
- Put the tuning in the provisioning playbook or image, give that a version, and declare the version. "Tuned per playbook v3" in every result is worth more than a list of settings nobody can vouch for.
- Pin with the work order's affinity setting, or with `taskset` or `numactl` in the script that calls benchx. Either way benchx records the mask the harness ran under.

**Builds**

- Build the same way every time: pinned toolchain, fixed flags, clean checkout. A compiler upgrade is a maintenance change like a kernel upgrade, and the Arrow story lists it among the things that silently start a new history.
- Do not build on the benchmark cores while benchmarks run. If the node builds for itself, builds and measurements alternate; if builds happen elsewhere, declare the build environment.

**Maintenance as an event**

- Around every maintenance change, re-measure a known commit before and after. A step in a series can then be attributed to the machine instead of being investigated as a regression. This is the practical answer to "telling the code got slower apart from the machine changed requires tribal knowledge" (Arrow story).
- Watch node health the same way. A node whose repeated measurements of an unchanged commit start to wander should be pulled, and its recent results annotated; the Arrow story's "misbehaving machine whose results have to be spotted and excluded by hand" is this case.

**Triggering**

- Treat pull-request code as untrusted. A long-lived tuned node that executes arbitrary contributor code needs a trigger restricted to trusted people, and isolation and cleanup between jobs. Both belong to the CI system.
- Keep precision settings the same for baseline and contender, and know what they cost. The Arrow local story notes that CI's short repetitions can be less precise than a careful local run; precision is a budget decision, and it is recorded in comparison context either way.

### 5.4 Cloud and ephemeral instances

Self-hosted runners provisioned on demand, as OpenBLAS does through cirun, are the practical way for a volunteer project to reach several architectures. They sit between hosted runners and dedicated nodes.

- Fix the instance type and declare it. For an ephemeral machine, identity is the instance type and image rather than the host, since no host outlives a job.
- Avoid burstable instance families. Their CPU allocation depends on a credit balance, which is invisible in the results.
- Prefer instance types that map to whole sockets or bare metal, and dedicated tenancy where the budget allows. Neighbors on the same host are the dominant noise source and cannot be detected from inside the guest.
- Expect each instance to be different hardware. Two instances of one type are two machines; series on such environments have a wider floor than on one physical node, and the comparator's noise estimate will reflect it.
- Bake the environment into an image and declare the image id. Installing dependencies at job start makes every job's environment slightly different.
- For weekly cron-style tracking on a handful of benchmarks, which is OpenBLAS's scale, this is a reasonable setup as long as comparisons across architectures are read as comparisons of those instance types, not of the architectures in general.

### 5.5 Accelerators

- Select devices in the work order, or in the calling script with `CUDA_VISIBLE_DEVICES` or the vendor's equivalent. A work order that requests a device the node lacks is refused. benchx records the selection as resource selection; on a multi-GPU host, results for GPU 0 and GPU 1 then share one host environment and differ in resource selection.
- Keep the device in a steady state across runs: driver persistence enabled, exclusive use by the benchmark, and clocks and power limits fixed where the hardware permits. Accelerators throttle thermally like laptops do, and the advice on interleaving and on distrusting long trends applies.
- Driver and runtime versions are observed context by default. A project whose subject is sensitive to them, such as one tracking kernel launch overhead, promotes them to identity in its policy.
- Device synchronization, JIT warmup, and on-disk kernel caches vary by array library and sometimes by workload variant. They belong to the harness and the timer (see the timer design), never to the environment setup. What matters for the environment is that the cache state is the same for both sides: either both cold or both warm, and declared.
- Partitioned devices such as MIG slices are resource selection; the partitioning scheme is part of the environment's identity.

### 5.6 Containers

A container is a good way to package L2 and a poor way to get a quiet machine.

- Declare the image digest. It is the most compact honest description of a software environment there is, and by default it is observed context.
- A container does not isolate performance. The host's kernel, governor, other tenants, and tuning apply unchanged, so everything in §5.3 still applies to the host.
- CPU quotas distort timing in ways that look like regressions. Give the container whole CPUs with a cpuset rather than a fractional quota. benchx records the cgroup limits it sees, so a quota at least does not go unnoticed.
- Profilers and hardware counters usually need capabilities that default container profiles withhold. Granting them is the operator's decision (`profiling.md`).

### 5.7 The software environment

- One environment per side. When two sides cannot share a process, as with `numpy==1.26.4` against `numpy==2.0` in UC-04, create both environments up front and run benchx in each. The alternation between them is then a loop in the calling script, since no harness can interleave across interpreters; pass `round` and `slot` through so the results record the realized order.
- For build-option comparisons, configure both build directories from one source tree with exactly one option differing, as the Arrow local story does for hardening, and declare the option.
- Record how the subject was installed: editable install, wheel, or in-place build. Performance differs between them more often than expected.
- State the BLAS and LAPACK vendor and their threading layer for anything numerical. It is the single most consequential environment fact in the NumPy, SciPy, and OpenBLAS stories, and it is not reliably detectable without a plugin.
- Datasets are part of the workload, not of the environment (schema §4.1), but where they live is environment: a dataset on a network filesystem measures the network.

## 6. When an environment changes

Environments change. The aim is that a change is visible, attributable, and does not pass for a change in the code.

| Change | What to do | How it shows up |
|---|---|---|
| OS, kernel, driver, or libc upgrade | upgrade between runs; re-measure a known commit before and after | observed context differs from that point on; the comparator carries the difference as a caveat (`comparator.md` §3) |
| compiler or toolchain upgrade | same; declare the new toolchain | subject configuration changes; a new series under the default policy, joined by a continuity mapping if the project chooses |
| tuning or playbook change | bump and declare the playbook version | environment metadata, or identity if the project's policy says so |
| hardware swap or new machine | overlap old and new on the same commits | a new environment; history continues only through an explicit continuity event |
| a thread cap or affinity that differs from last time | usually an accident: fix the calling script | comparison context differs, so results land in a different series rather than silently blending |

Continuity events and their audit trail are specified in a separate design document (schema §4.3).

## 7. What to declare: a checklist

Facts benchx cannot detect reliably, in rough order of how often their absence has made a result useless in the collected stories:

| Declare | Why |
|---|---|
| build configuration behind a build directory (build type, flags, feature toggles, compiler) | "hardened versus plain" is otherwise two paths (Arrow local) |
| BLAS and LAPACK vendor, version, threading layer | dominates numerical results (NumPy, OpenBLAS) |
| install method of the subject and of pinned dependencies | a version does not identify a build (schema PR discussion; pandas story) |
| stable runner name | survives host-name changes and reinstalls (Arrow) |
| instance type, tenancy, image id | the identity of an ephemeral machine (OpenBLAS) |
| provisioning playbook or image version | makes the tuning state citable |
| container image digest | compact description of L2 |
| what an environment label means | `numpy-2.0` is a label; the pinned component is the fact (UC-04) |
| cache state for JIT or kernel caches | cold and warm runs are different measurements (Array API) |

## 8. Consequences for other design documents

This document removes building from the runner and limits enforcement to the process the runner launches. The following agree with it, or are amended to:

- **`runner.md`:** "resolve target" becomes read-only identification of a prepared target: an existing build directory, working tree, environment, or artifact. Build caches and build failure as an outcome are removed. The environment policy stays: the runner applies its `enforce` rules to the launched process, verifies the `verify` rules, and refuses a run whose requested hardware or conditions are unavailable. The work order names a prepared target, never a revision to build.
- **`runner-schema.md`:** target kind `revision`, `build.cache`, and the `build-failed` outcome are removed, and the `build` work-order field now only declares the prebuilt target's configuration; the environment-policy model (§4) stands, with `enforce` scoped to the launched process. The two documents no longer conflict.
- **`system-decomposition.md` §3.3:** "obtains or builds the target" becomes "runs against a prepared target". "Controls and records the execution environment" stays, read as the launched process.
- **`harness-adapter.md` §3 and §5:** the context-document table's "the runner's environment policy" stands. The driving half still applies nothing to the environment itself and passes it through; enforcement of launch settings is the runner's, around any adapter.
- **`benchmark-result-schema.md` §4.2:** "the job of the runner or adapter that prepares the environment" becomes "the job of whoever prepares the environment; the runner or adapter records the facts".
- **`prototype-scope.md`:** building revisions moves to *out of scope*. Environment policy enforcement stays *deferred*.
- **UC-03:** the two revisions are built by the project's target provider (`spin`, a CI script), called by the workbench or CI job; benchx receives two prepared targets, which makes UC-03's setup the same shape as UC-04's. `spin bench` without `--compare` (UC-01) is one prepared target, in the current environment, and may be dirty.

## 9. Open questions

1. How is the source of a fact (§3.4) represented in the message: a marker per field, or one provenance map from field path to `declared`, `detected`, or a plugin name?
2. Recording a realized value as the intended one lets an accidental setting, such as a leftover `OMP_NUM_THREADS`, enter comparison context and start a new series. That is visible, which is the point, but noisy. Should undeclared settings stay in observed context until a project's policy promotes them?
3. Which harnesses can interleave sides on their own, and what do UC-02 and UC-03 look like on one that cannot? For UC-04 the loop is necessarily the caller's; for UC-03 it is the workbench's session loop (`system-decomposition.md` §3.4).
4. Where do declarations live: in the work order, in a per-project configuration file, in a per-node file an operator maintains, or all three with a defined precedence?
5. ~~Is the default allowlist of environment variables (§3.1) part of the core, or does each adapter contribute the variables its ecosystem cares about?~~ Both. The core holds the default, a project extends it, and an adapter adds the variables its ecosystem cares about; the runner composes one list and every record of the environment, including artifacts an adapter keeps, is limited to it.
6. Should the zero-configuration snapshot be available as a stand-alone command, so an operator can see what benchx would record about a machine before running anything on it?
