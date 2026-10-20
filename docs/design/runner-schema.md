# Runner Schemas: Work Order, Environment Policy, Run Context

**Status:** Draft for review. Aligned with `benchmark-environments.md` (§2.2, §2.3, §8): the runner receives a prebuilt target and never builds, it enforces only on the process it launches, and it verifies what the work order requests, refusing the run when it cannot be delivered. This document previously carried a target kind that checked out and built a revision, a build cache, and enforcement of machine state; those are removed.

**Companion to:** `runner.md` (PR #24), `benchmark-result-schema.md`, `benchmark-environments.md`

## 1. Purpose and scope

This document proposes three versioned schemas for the runner described in `runner.md`: the **work order** (the runner's input), the **environment policy** (referenced by the work order), and the **run context** (the conventions the runner uses when filling each result). Together with the existing measurement-result schema (`schemas/measurement-result/0.1.0`), they make up the runner's whole contract.

- **In scope:** everything a single runner invocation on one compute node reads or writes.
- **Out of scope:** scheduler state, store ingestion APIs, comparison verdicts, and harness-native output formats (adapters own those).
- **Non-goal for 0.1.0:** benchmarks that span several nodes. A work order targets exactly one node; many independent work orders across a fleet are fine. See §8.

## 2. Principles

Each schema rule below traces to a principle in `runner.md` or a rule in `benchmark-result-schema.md`.

1. **Work orders are documents, not calls.** A work order is plain JSON with a `schema_version`, a `work_order_id`, and a content hash. It can be written by hand, emitted by a scheduler or CI job, stored, and replayed. The runner reads nothing else.
2. **Self-contained means resolved.** Everything that changes the run is in the order or in a policy it references by name *and* version. Ambiguous references such as "latest" or a bare branch name are allowed only in a draft and resolved to fixed values before execution; the resolved order is what gets hashed and cited.
3. **No new result fields.** The runner adds nothing to the measurement-result schema. What it captures goes into slots that already exist: `coordinates.environment`, `coordinates.subject.configuration`, `procedure`, and `provenance`, plus sibling results for condition measurements.
4. **The work order is the run manifest.** `benchmark-result-schema.md` §5.1 leaves "lost vs. never planned" to the runner design. Here the resolved order lists every case the run plans, so a missing result can be told apart from one that was never requested.
5. **Every observation says whether it was available.** A fact the runner tried to read but could not is recorded as `null`, and a condition it could not measure as a `skipped` measurement with reason `probe-unavailable`; neither is dropped. "No temperature on this VM" and "forgot to record temperature" look different.

## 3. Work order

A work order (`benchx/work-order/0.1.0`) has four required groups matching `runner.md` §3: **what**, **at what**, **how**, **for whom**. Every field says where it ends up in the result, so nothing the order asks for disappears without a trace.

| Field | Type | Req. | Group | Meaning | Lands in result as |
|---|---|---|---|---|---|
| `schema_version` | const | yes | — | `benchx/work-order/0.1.0` | — |
| `work_order_id` | URI or UUID | yes | — | Stable name for this order | `provenance.artifacts` (kind `work-order`, with sha256) |
| `state` | `draft` \| `resolved` | yes | — | Only `resolved` orders execute (§3.2) | — |
| `project` | token | no | what | Project in the store; omit for an ad hoc run (results are then thin) | `project` |
| `suites[]` | array | yes | what | `{adapter, suite, filter, include[], exclude[], parameters}` per harness suite; `filter` is a harness-native string, an alternative to `include`/`exclude` for adapters with only single-pattern selection | `coordinates.workload` via the adapter |
| `quantities[]` | array of names | yes | what | Quantities to collect, e.g. `wall-time`, `peak-rss` | `measurement.quantity` |
| `environment_variables` | string map | no | what | `{NAME: value}` set on the measured process, on top of what the runner inherited; a workload coordinate, not a machine fact — `OMP_NUM_THREADS=1` and `=10` are different, non-pooling variants | `coordinates.workload.parameters` |
| `workload_parameters` | map | no | what | Parameters passed to the runner or harness, never as environment variables; a name may not appear in both maps | `coordinates.workload.parameters` |
| `target` | one of 3 kinds | yes | at what | What code to measure (§3.1) | `source`, `revision`, dirty flags, tree ids |
| `benchmark` | object | no | at what | `{source, revision}` naming where the benchmark suite's own code lives, when different from `target` (§3.3, added here to close a gap) | top-level `benchmark.source`, `benchmark.revision` |
| `components[]` | array | no | at what | Pinned non-primary components `{name, role, source, revision}` | `coordinates.subject.components` |
| `build` | object | no | how | `{profile, type, compiler, flags[], options{}}`: the **declared** configuration of the prebuilt target, for facts the runner cannot read from it. Never an instruction to build | `coordinates.subject.configuration` |
| `precision` | object | yes | how | `{repetitions, calibration, warmup}`, spelled per Appendix B (`repetitions` a bare int or `{mode: fixed, levels[]}`; `calibration.mode`: `adaptive` + `minimum_sample_seconds`, or `fixed` + `n_iterations`; `warmup.mode`: `none`, `count` + `n_warmup`, or `time` + `seconds`) | intended: `protocol`; realized: `procedure` |
| `schedule` | object | no | how | `{kind: sequential \| alternating \| random, seed}` across sides | `protocol.schedule`; realized `procedure.slot` |
| `timeouts` | object | yes | how | `{case_s, order_s}`, both positive | exceeded: `censored` or `error` result |
| `environment_policy` | `{name, version}` | no | how | Named policy (§4); absent means the runner applies none and only records the environment system and condition measurements, the default `benchmark-environments.md`'s laptop scenario describes | policy identity in `coordinates.environment.identity` |
| `run_key` | token | yes | for whom | Groups this order with sibling orders in one comparison | `provenance.run_key` |
| `round` | integer | no | for whom | Position in an interleaved run; set by the run author | `procedure.round` |
| `labels` | string map | no | for whom | Caller labels, e.g. `{"env": "numpy-2.0"}` | `provenance.labels` |
| `requester` | `{kind, name}` | yes | for whom | `kind`: `scheduler` \| `ci` \| `workbench` \| `person` | `provenance.info.requester` |
| `reason` | string | no | for whom | Free text: why this run exists | `provenance.info.reason` |
| `plan[]` | array | resolved only | — | Every case × quantity the run will attempt | the run manifest (§6) |

### 3.1 Target kinds

| `target.kind` | Required fields | What the runner must verify |
|---|---|---|
| `working_tree` | `path`, `source.uri`; optional `source_dir` | Records HEAD, dirty state, and tree id as found at `source_dir` (default `path`); never cleans or checks out the tree |
| `build` | `path`, `source.uri`; optional `source_dir` | Build config and staleness vs. the tree at `source_dir`, when it differs from what the build itself records (`runner.md` open question 2) |
| `artifact` | `uri`, `sha256`, `source.uri`, `revision` | Checksum matches; `revision` is the artifact's declared source, taken on trust and flagged in `quality.warnings` |

All target kinds name something that already exists; none causes a checkout, configure, or build. `source.uri` names the canonical repository; `source_dir`, when given, is the local checkout path the runner inspects for revision/dirty/tree facts. Absent, the runner falls back to whatever the target itself records (e.g. a build directory's own `CMAKE_HOME_DIRECTORY`).

Every target kind also takes four optional fields of user-owned provenance text, copied unchanged from the target description a target provider returned (`system-decomposition.md` §3.4, `schemas/target-description/0.1.0`):

| Field | Meaning | Lands in result as |
|---|---|---|
| `how_built` | what the user did to build the target: the commands, or the build script's name | `provenance.info.how_built` |
| `activation` | what the user ran to enter the target's environment: sourcing a script, loading modules, exporting library paths | `provenance.info.activation` |
| `shell` | the shell `activation` is written for, such as `bash` | `provenance.info.shell` |
| `provider` | `{name, version}` of the target provider that produced the description | `provenance.info.target_provider` |

All four are declared facts (`benchmark-environments.md` §3.2): the runner records them as given, never executes them, and never checks them against anything. The user activates the environment before invoking benchx, and the runner records the environment it inherited (§5.3 `env`) beside the declaration, so a reader sees both and the runner picks no winner. Absent, null, empty, and whitespace-only all mean not declared, and the runner omits the key. None of the four enters identity, so rewording a recipe never splits a series. They are copied verbatim into results, which may reach a shared store, so they must not hold secrets.

### 3.2 Draft and resolved orders

A person or tool may write a **draft**: a branch name instead of a commit, `include` globs instead of cases, a policy without a version. Before execution it becomes a **resolved** order, with full commit ids, a pinned policy version, and an expanded `plan`. Results cite the resolved order's hash, never the draft's.

Resolution happens as early as possible, and the runner fills in only what is left:

- **Resolution only fills gaps.** It never changes a field that is already fixed, so a fully resolved order passes through the runner unchanged.
- **The requester resolves shared facts.** The scheduler, CI job, or workbench fixes everything that must be identical across sibling orders in a `run_key`: commit ids for branch names, policy versions, precision defaults, and the case list when it is knowable without running the target. Resolving these once is what keeps both sides of a comparison on the same values.
- **The runner resolves only facts about its own machine.** These are working-tree state (HEAD, dirty flag, tree id), the case list for harnesses that enumerate cases only by listing them from the prebuilt target, and the values actually enforced by the policy. It then records the resolved order as a provenance artifact before execution starts. A `plan` entry the runner filled in this way carries `from`, the suite's `filter` or `include` pattern it was expanded from, so a runner-resolved entry stays traceable to what generated it, distinct from one a scheduler hand-authored.
- **The runner refuses unresolved shared fields.** A draft that still names a branch, or a policy without a version, is refused rather than resolved against the runner's local clone. That would infer the run from leftovers on the machine, which `runner.md` principle 1 forbids.

So a person on a laptop can hand the runner a loose draft whose target is `working_tree`, which is only meaningful on that machine anyway, while a fleet order arrives already pinned.

Where results are delivered (store URL, local file) is an invocation argument, not an order field. Replaying an order on another machine shouldn't silently post to the original store.

### 3.3 Benchmark suite location (addendum)

§3's field table and §3.1 name only the subject's target. Nothing in the original draft says where the *benchmark* suite's own code lives, yet every non-thin result requires `benchmark.source` and `benchmark.revision` (`benchmark-result-schema.md` §4.1, §5.2) alongside `provenance.benchmark_dirty`/`benchmark_tree`. This is a gap in the original design, closed here: an optional `benchmark` object, `{source, revision}`, mirroring `sourceRef` and a bare revision key the way `target.kind: revision` does. When present, the runner records that revision the same way it records `target`'s, from the benchmark checkout it is pointed at; it does not fetch it. When absent, the benchmarks are assumed to live in the same checkout as `target`, and the runner reuses `target`'s resolved source, revision, dirty flag, and tree for `benchmark.*` and `provenance.benchmark_dirty`/`benchmark_tree` — the common case where a project's benchmarks live alongside its own code. A benchmark suite with no version control is covered for a single file: `benchmark.revision` is then absent, `benchmark.source` is the `file://` URI of the file's directory (`type: directory`), `provenance.benchmark_tree` is the file's git blob id (`git hash-object FILE`), and `benchmark_dirty` is `dirty`. The file, not its directory, is the unit hashed, so what it imports is not covered. A suite inside another git repository takes that repository's source and HEAD as its revision, with the file's own blob id as the tree and `clean` when the file equals HEAD's version. The prototype applies this to suites that are source files (pyperf); a suite that is a built binary (Google Benchmark) is the subject's own. The runner finds the suite's identity itself, whatever the order says, and refuses an order whose `benchmark` disagrees with it. A `file://` source names a place on one machine, so such a series does not follow the suite to another machine or folder (the prototype's identity keeps the benchmark's source URI).

## 4. Environment policy

A policy (`benchx/environment-policy/0.1.0`) is a named, versioned list of rules. Each rule has a **check**, a **tier**, parameters, and an `on_fail` action. The tier sets both what the runner does and where the fact lands in the result.

| Tier | Runner action | On failure | Lands in result as | Enters identity? |
|---|---|---|---|---|
| `enforce` | Sets the condition on the launched process before execution; never changes machine state | Refuse the order | `coordinates.environment.identity` (policy name + version + enforced values) | Yes |
| `verify` | Checks before, and optionally after, execution | `refuse` or `warn`, per rule | Value read in `environment.system`, or a condition measurement; failures in `quality.warnings` | No |
| `record` | Observes only | Never fails; unreadable = `null` or a `skipped` measurement | `environment.system` or condition measurements | No |

Changing a policy's version changes environment identity, so it splits the series: the "series-splitting event, not a silent change" of `runner.md` §5.

### 4.1 Core check vocabulary

The core set is small and CPU/GPU generic. Project-specific checks use a namespaced prefix (`x-arrow.*`, `x-cupy.*`) so `runner.md` open question 1 can be settled case by case without a schema change.

| Check | Parameters | Laptop policy | Tuned-node policy |
|---|---|---|---|
| `threads.max` | `n` | enforce | enforce |
| `cpu.affinity` | CPU set | — | enforce |
| `cpu.governor` | e.g. `performance` | record | verify, refuse |
| `cpu.boost` | on/off | record | verify off, refuse |
| `cpu.smt` | on/off | record | verify |
| `gpu.device` | index or UUID | enforce | enforce |
| `gpu.clocks-locked` | MHz | — | verify, refuse |
| `accel.sync` | `events` \| `device-sync` | enforce | enforce |
| `load.quiescent` | max 1-min load, window s | record | verify, refuse |
| `hardware.present` | CPU model pattern, GPU count, min RAM | verify, warn | verify, refuse |
| `tree.clean` | — | record | verify, refuse |
| `thermal.throttle` | counters to diff | record | verify, warn |
| `thermal.temperature` | sensors, sample interval s | record | record |

Only conditions of the launched process (`threads.max`, `cpu.affinity`, `gpu.device`, `accel.sync`) can be `enforce`; machine state (`cpu.governor`, `cpu.boost`, `cpu.smt`, `gpu.clocks-locked`) is `verify` or `record`, because an operator sets it and the runner does not. A laptop policy mostly records; a tuned-node policy mostly verifies and enforces on the process. Both produce results in the same shape, which is what lets one runner serve both.

### 4.2 Refusal is still a result

`runner.md` §6 says an unsatisfiable policy refuses the run "with the reason reported". Principle 5 of the same document says every outcome is a result. This schema reconciles the two: a refusal emits one `error` result per `plan` entry with `measurement.reason = "policy-unsatisfied-<check>"`. Nothing is measured, but nothing goes missing either.

## 5. Run context

The runner fills existing measurement-result fields and defines no new ones. What it does define are two conventions for the open objects: an environment identity schema and a set of condition measurements.

### 5.1 Identity and revision

| Fact | Result field | If unknown |
|---|---|---|
| Subject commit | `revision.key` | Order refused; no measurement without a revision |
| Subject dirty state | `provenance.subject_dirty` | `unknown`, never `clean` by default |
| Subject tree hash | `provenance.subject_tree` | Omitted; comparators then treat it as not code-identical |
| Benchmark code dirty state / tree | `provenance.benchmark_dirty`, `benchmark_tree` | as above |
| Build configuration | `coordinates.subject.configuration` | `quality.warnings`: `build_config_unverified` |
| Runner software | `provenance.runner` `{name, version}` | never unknown |
| Resolved work order | `provenance.artifacts` (kind `work-order`, URI + sha256) | never unknown |

### 5.2 Environment identity: `benchx/env-identity/0.1.0`

This goes in `coordinates.environment` with `schema = "benchx/env-identity/0.1.0"`. A fact goes in `identity` when it names the machine, and in `system` otherwise; whether either splits history is the identity policy's choice (`benchmark-result-schema.md` §4.3).

- **identity:** `machine_id` (salted hash of `/etc/machine-id` or equivalent), `cpu.model`, `cpu.logical_cores`, `memory_bytes`, `gpus[] {model, memory_bytes}`, `os.family`, `virtualization` (`bare-metal` | `vm` | `container` | `wsl`), `policy` (`name@version`), `enforced{}` (the values actually applied)
- **system:** hostname, GPU UUIDs, BIOS/firmware strings, and the fields of §5.3

GPU UUIDs stay out of identity, because swapping a card of the same model shouldn't split history. The device each result used goes in `coordinates.resource_selection`.

### 5.3 System and condition measurements

The runner reads two kinds of thing about the machine, and they land in two places (`benchmark-result-schema.md` §4.2).

**The system** is constitutive and fixed for the attempt. Its fields go in `coordinates.environment.system`, nested by group, and are copied to every result of the attempt. A fact the runner tried to read and could not is `null`; a fact it did not try to read is absent.

| Field | Typical source (Linux) |
|---|---|
| `os.kernel`, `os.release` | `uname`, `/etc/os-release` |
| `libc.name`, `libc.version` | `ldd --version`, `gnu_get_libc_version` |
| `cpu.microcode` | `/proc/cpuinfo` |
| `cpu.governor`, `cpu.boost`, `cpu.smt` | cpufreq and SMT sysfs |
| `gpu.driver`, `gpu.uuids`, `gpu.clocks_locked_mhz` | NVML or the vendor equivalent |
| `container.image_digest` | runtime |
| `runtime.*` | the interpreter or VM the harness runs on |
| `process.environment`, `process.cgroup` | the launched process, allowlisted (`benchmark-environments.md` §3.1) |

**Condition measurements** are states the machine passes through. Each is a sibling result for its own quantity, sharing the measurand's `attempt_key`, with the probe and how it was read in `protocol.probe` (`benchmark-result-schema.md` §4.4). A probe that cannot read on this machine yields status `skipped` with reason `probe-unavailable`.

| Quantity | Unit | Typical source (Linux) | `probe.sampling.mode` |
|---|---|---|---|
| `cpu-frequency` | `MHz` | cpufreq sysfs | `before-attempt`, `interval` |
| `load-average` | `1` | `/proc/loadavg` | `before-attempt`, `after-attempt` |
| `cpu-temperature`, `gpu-temperature` | `Cel` | hwmon or thermal zones; NVML for GPUs | `interval` |
| `throttle-events` | `1` | throttle counters, after minus before; NVML throttle reasons | `delta` |

Throttle evidence is recorded separately from temperature. It's the signal that can explain a slower result, and it's available on some machines that don't expose temperature.

## 6. Outcome statuses

Every `plan` entry ends as exactly one result, using the five statuses the result schema already has. Each result carries its entry id in `provenance.info.plan_entry`. A plan entry with no result therefore means the runner itself died: the run was **lost**, not skipped.

| Runner outcome | Stage | `measurement.status` | `measurement.reason` | Extra evidence |
|---|---|---|---|---|
| All requested repetitions completed | execute | `success` | — | — |
| Some repetitions failed or were cut short | execute | `partial` | `repetitions-incomplete` | `procedure.completed_repetitions` |
| Case exceeded `timeouts.case_s`, time quantity | execute | `censored` | `timeout-case` | constraint `lower_bound` = `case_s`, `cause` = `timeout` |
| Case exceeded `timeouts.case_s`, other quantity | execute | `error` | `timeout-case` | — |
| Harness explicitly skipped the case | execute | `skipped` | harness's own reason | — |
| Harness crashed mid-suite (remaining entries) | execute | `error` | `harness-crashed` | log in `provenance.artifacts` |
| Adapter could not translate output | emit | `error` | `adapter-error` | raw output as artifact |
| Target missing or not identifiable (no build directory, checksum mismatch) | identify target | `error` (every entry) | `target-unavailable` | detail in `provenance.info` |
| Policy check failed with `refuse` | prepare | `error` (every entry) | `policy-unsatisfied-<check>` | value read in `environment.system`, or the condition measurement |
| `timeouts.order_s` exceeded (unstarted entries) | any | `error` | `timeout-order` | — |
| Policy check failed with `warn` | prepare / after | unchanged | — | `quality.warnings` entry |
| Runner process killed | any | no result | — | resolved order shows the gap |

Reasons are drawn from a closed list of codes, optionally followed by a hyphen and a detail, so dashboards can group failures by prefix without parsing free text.

**Flagged issue (found while fixing the token-regex bug below).** The result schema's `measurement.reason` pattern is `^[a-z0-9]+([.-][a-z0-9]+)*$` (`schemas/measurement-result/0.1.0/schema.json`) — lowercase alphanumeric segments joined only by `.` or `-`. Two things above violated it and are corrected in this revision: the codes used `_` (now `-`, matching `harness-adapter.md`'s own `harness.no-output`-style reasons), and `policy_unsatisfied:<check>` used `:` as a code/detail separator, which the pattern permits nowhere — no delimiter was ever reserved for it. The fix joins code and detail with `-` instead (`policy-unsatisfied-<check>`), which validates, but weakens the original intent: §7's "closed list of codes, optionally followed by a detail, so dashboards can group failures without parsing free text" assumed an unambiguous boundary between the fixed code and the open check name. Joined by the same character multi-word codes already use, that boundary survives only for a human who knows the closed code list, not for an automated grouping-by-prefix reader. `<check>` must itself be hyphen-safe for the composed reason to validate, which is also why `gpu.clocks_locked` in §4.1 is corrected to `gpu.clocks-locked`. A structured alternative — keeping `policy-unsatisfied` as the whole `reason` and carrying `<check>` in a separate field (a new `constraint`-adjacent field, or `provenance.info`) instead of concatenating it — would preserve the separation properly; that's a result-schema change and is left open rather than decided here.

## 7. Versioning and hashing

All three schemas use the same scheme as `schemas/measurement-result/0.1.0`: semver in a directory path, with JSON Schema 2020-12 files and examples beside them.

| Schema | Path | Hash used for |
|---|---|---|
| Work order | `schemas/work-order/0.1.0/` | citing the resolved order from results |
| Environment policy | `schemas/environment-policy/0.1.0/` | detecting a policy edited without a version bump |
| Env identity | `schemas/env-identity/0.1.0/` | referenced by `coordinates.environment.schema` |
| Identity policy | `schemas/identity-policy/0.1.0/` | `identity_schema` names the policy and enters `comparison_fingerprint` (`benchmark-result-schema.md` §4.3) |

Orders and policies are hashed the same way the result schema hashes its fingerprints:

```text
work_order_sha256 = SHA-256("benchx-work-order-v1\0" + JCS(resolved_order))
policy_sha256     = SHA-256("benchx-env-policy-v1\0" + JCS(policy))
```

- **Only resolved orders are hashed.** A draft has no stable meaning, so it has no hash.
- **A policy's `(name, version)` is bound to one hash.** The store rejects a second policy body under the same name and version. That enforces "loosening a policy splits the series" in the store rather than by convention.
- **Compatibility:** a runner must refuse an order whose major `schema_version` it doesn't know. Minor versions only add optional fields, and older runners ignore them only if the field is in the schema's `ignorable` list. Otherwise they refuse, because silently ignoring a precision or policy field would produce a result that misdescribes itself.

## 8. Worked example: Arrow on a laptop

A contributor measures an uncommitted Parquet change on a WSL2 laptop: the local story from `docs/user_stories/apache-arrow-local-benchmarking.md`, using the names from `schemas/measurement-result/0.1.0/examples/arrow.json`.

**Resolved work order**

```json
{
  "schema_version": "benchx/work-order/0.1.0",
  "work_order_id": "urn:uuid:6f1c2b7e-3d4a-4e8b-9a51-0c2d7e9f4b13",
  "state": "resolved",
  "project": "arrow",
  "suites": [{"adapter": "google-benchmark", "suite": "parquet-read",
              "include": ["parquet-read/snappy"], "parameters": {}}],
  "quantities": ["wall-time"],
  "target": {"kind": "working_tree", "path": "~/arrow",
             "source": {"uri": "https://github.com/apache/arrow", "type": "git"}},
  "build": {"profile": "ninja-release", "type": "release", "compiler": "clang-18"},
  "precision": {"repetitions": 5, "min_time_s": 0.01, "warmups": 1},
  "timeouts": {"case_s": 60, "order_s": 1800},
  "environment_policy": {"name": "laptop-default", "version": "1.0.0"},
  "run_key": "local:parquet-snappy-2026-09-23",
  "round": 1,
  "labels": {"side": "contender"},
  "requester": {"kind": "workbench", "name": "local"},
  "reason": "check snappy decode change before opening PR",
  "plan": [{"id": "p0", "case": "parquet-read/snappy", "quantity": "wall-time"}]
}
```

**Runner-owned fields of the resulting `success` result** (everything else comes from the adapter)

```json
{
  "revision": {"key": "02addad7a1f3ce7d6de6a6230ec4c17828cb8f52"},
  "coordinates": {
    "subject": {"configuration": {"build_type": "release", "compiler": "clang-18"}},
    "environment": {
      "schema": "benchx/env-identity/0.1.0",
      "identity": {"machine_id": "sha256:3e1f...", "cpu": {"model": "Intel Core i7-1260P", "logical_cores": 16},
                   "memory_bytes": 17179869184, "os": {"family": "linux"}, "virtualization": "wsl",
                   "policy": "laptop-default@1.0.0", "enforced": {"threads.max": 1}},
      "system": {"os": {"kernel": "6.6.87.2-microsoft-standard-WSL2"}, "cpu": {"governor": null}}
    }
  },
  "provenance": {
    "run_key": "local:parquet-snappy-2026-09-23",
    "labels": {"side": "contender"},
    "subject_dirty": "dirty",
    "subject_tree": "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "runner": {"name": "benchx-runner", "version": "0.1.0"},
    "artifacts": [{"kind": "work-order", "media_type": "application/json",
                   "schema": "benchx/work-order/0.1.0",
                   "uri": "file:///home/me/.benchx/orders/6f1c2b7e.json", "sha256": "..."}],
    "info": {"plan_entry": "p0", "requester": "workbench:local",
             "reason": "check snappy decode change before opening PR"}
  }
}
```

The runner also emits sibling results under the same `attempt_key`: `load-average` with one observation of `0.42` (`before-attempt`), and `cpu-temperature` and `throttle-events` with status `skipped` and reason `probe-unavailable`. WSL2 exposes no governor, temperature, or throttle counters, so the governor fact is `null` and the two probes are skipped. The dirty tree makes any comparison using this result local-only (`benchmark-result-schema.md` §5.5).

## 9. Open questions

1. **Multi-node benchmarks.** 0.1.0 targets one node per order. Should a later version allow a node group (`target.nodes[]` plus a coordinator role), or should a distributed benchmark stay a harness concern behind a single coordinator runner?
2. **Who stores resolved orders?** The order doubles as the run manifest, but the runner is store-unaware. Does the runner upload the order as an artifact alongside results, or does the requester (scheduler, workbench) keep it?
3. **Case listing failure before the plan exists.** §3.2 lets the runner expand `plan` by listing cases from the prebuilt target for harnesses that enumerate them only that way. If listing fails, or the target is unidentifiable, there are no plan entries to attach `error` results to. Does the runner emit a single order-level `target-unavailable` result, and under which workload coordinates?
4. **Sampling overhead.** Sampling temperature and frequency during execution can itself disturb the measurement. What default interval (e.g. 1 s) and core placement keep it negligible, and should the policy be able to turn sampling off?
5. **Delivery in the order?** This draft makes the result destination an invocation argument. Are there cases, such as CI, where the order should carry it?
6. **Refusal as results.** §4.2 turns a policy refusal into one `error` result per plan entry, which goes beyond `runner.md` §6's "refused before execution". Keep that, or report refusals only to the requester?
