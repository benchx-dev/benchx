# Prototype Design

**Status:** Draft for review
**Companion to:** `prototype-scope.md` (what's in the slice); `benchmark-result-schema.md` (the result); `runner-schema.md` and `schemas/work-order/0.1.0` (the work order); `harness-adapter.md`; `benchmark-environments.md`; `comparator.md`; `runner.md`; `system-decomposition.md` §3.4 (the workbench); `measurement-result-parquet.md` (#30, the result row)

## 1. Shape

One command-line tool over a Python API, a local store that is a single
Parquet file, three document formats. No server, no daemon: the transport is
the file system, which the ingest contract (schema §5.4) already accepts as a
conforming delivery path. Every arrow in the slice is a file a reviewer can
open. JSON is for documents in transit, the work orders, result files, and
comparison documents that pass between components; the store is Parquet only,
one file any Arrow-capable tool can read.

```
bx run <order.json> [--out DIR] [--no-ingest]
                                         # runner + adapter → DIR/*.json, delivered to the store
bx ingest <dir|file>                     # sweep documents into the store
bx series [--workload … --quantity …]    # list series
bx history <series>                      # one series' points (a query)
bx head [-n 10] [--project P] [--workload W] [--json|--table]  # the latest results, newest first
bx compare --run KEY --profile revisions|environments --baseline VALUE
           [--label NAME] [--results DIR] [--k 3] [--min-rounds 3] [--json]
                                         # read mode: compare results already measured
bx target describe|prepare TARGET        # a target's description (a provider's answer, or a build directory's sidecar)
bx compare BASELINE CONTENDER --profile revisions|environments
           [--adapter pyperf|google-benchmark]                  # default: adapter.name
           --suite NAME [--filter REGEX] [--quantity NAMES]     # default: adapter.suite, adapter.quantities
           --rounds R [-e NAME=VALUE ...]
           [--repetitions N]                                    # google-benchmark
           [--processes N] [--values N] [--loops N] [--warmups N] # pyperf
           [--min-time S]
           [--project P | --no-project] [--label NAME=BASE,CONTENDER]
           [--run-key KEY] [--out DIR] [--no-build] [--source-uri URI]
           [--k 3] [--min-rounds 3] [--json]
                                         # measure mode: run both sides, then compare
bx bench TARGET --suite NAME [CASE …]    # the single-target form of bx compare: one order, a table
         [--adapter …] [--filter REGEX] [--quantity NAMES] [-e NAME=VALUE ...]
         [precision flags, as for bx compare]
         [--project P | --no-project] [--run-key KEY] [--out DIR] [--no-build] [--source-uri URI]
         [--label NAME=VALUE ... --round N --slot K]   # one side of a session other calls join
         [--case-timeout S] [--no-ingest]
bx bench MODULE.py [FUNCTION …] [--tree DIR] [...]
                                         # shorthand: the module's checkout, pyperf → result files → store → table
bx config init [--dir DIR] [--adapter …] # write a benchx.conf.json stating a harness's defaults
bx config show                           # what benchx.conf.json and the defaults give, and where each value comes from
```

`bx bench` is the single-target form of `bx compare` (measure mode). It names a
target the same way, takes the same options, and goes through the same code:
the side is resolved by `session._side` (a provider call for `@CONFIG`, else a
bare path the harness reads; the adapter checks the suite before anything runs)
and the order is built by `session._order`. A bench run is one order of a
comparison session with no comparison, and by default no round or slot; it is run by the
same runner, delivered as `bx run` delivers (`--no-ingest`: files only), and
summarized as a table of its cases. It is not `bx run`: that executes a finished
order file and never resolves a target; `bx bench` turns options into an order
(saved as `results/RUN_KEY/orders/order.json`, which `bx run` can replay) and
then does what `bx run` does.

**A call as one side of a session (UC-04).** Environments that cannot share a process, such as two
venvs holding different numpy versions, are measured by separate `bx bench` calls that share a
`--run-key`; the caller's script is the loop (`benchmark-environments.md` §5.7). `--label NAME=VALUE`
names the call's side, `--round N` pairs its attempt with the other side's, and `--slot K` is its
position in the alternation, which the caller counts. Each call writes `orders/order-KKKK.json`. The
flags need an explicit `--run-key` (the default is a timestamp per call, so two calls would never
share a run), and `--label` and `--round` need `--slot` (the ingest key gets its `:slot-K` suffix only
when a slot is set, so two sides would otherwise share a key). `bx compare --run KEY --profile
environments --label NAME --baseline VALUE` then compares the sides, and refuses a run whose sides do
not alternate, have unequal attempt counts, or do not differ.

What differs between the environments is recorded by the user's own commands, so benchx knows no
environment kind: a context collector (§3) prints facts about the environment it runs in, which land
under `environment.system.<name>`, a part of the varying coordinate; a setup file does the same for
declared facts. A collector with `verify: how_built` and `on_fail: warn` checks the facts a target's
`how_built` object declares against what it found, and the comparison lists the warning beside the
facts that differ. `prototype/examples/numpy-venvs` is the worked example, with the map from UC-04's
spellings to these.

A bare path is read by the harness. For Google Benchmark it is
`BUILD_DIR[:SOURCE_DIR]`, a `build` target. For pyperf it is a working tree, a
`working_tree` target that is its own source, measured as found with its dirty
state and tree id recorded; `--suite` is the module's path in it, and a case is
a function in it (`bench_*` and `benchmark_*` by default). A case named on the
command line is an exact name; `--filter` is a regex; not both. `-e` fills
`environment_variables`. Precision is resolved before the order is written,
flags over `adapter.precision` in `benchx.conf.json` over the harness's defaults, so the
order always states what applied; a flag the chosen harness does not take is refused, as in
`bx compare`. The pyperf adapter takes `python` from the `PATH` the workers
start with and refuses the order if pyperf is not importable there. Both
commands default to pyperf.

`bx bench MODULE.py [FUNCTION …]` is a shorthand for the pyperf form: the target
is the git checkout containing the module (or `--tree`), the suite is the
module's path in it, and the functions are exact case names. Without `--suite`,
a TARGET ending in `.py` selects it.

**The project file.** Both commands read `benchx.conf.json` in the project root, found by
walking up from the working directory; its directory is the root, and with no file the
root is the working directory (`schemas/config/0.1.0`). It holds `project`, `source.uri`,
`provider.command`, an `adapter` block (`name`, `suite`, `quantities`, `precision`),
`environment_variables`, `collectors`, `timeouts`, `paths.store` and `paths.results` (default
`store.parquet` and `results/` in the root, and relative to the root), and `compare`
(`profile`, `rounds`, `k`, `min_rounds`). A command-line argument wins; if it is missing the
value is read from the file; if that is missing too, a built-in default applies, where one
exists. A required value with none of the three (`adapter.name`, `adapter.suite`, the
target, and for `bx compare` the profile and the rounds) is a hard failure that names the
flag and the key. `--adapter` naming a harness other than the file's is refused, since the
file's suite and precision belong to its own. `--quantity` takes a comma-separated list
and one run collects all of them. `--no-project` makes the run ad hoc, filed in the store
as `local/<hostname>`, whatever the file's `project` says. The provider is needed only by an
`@CONFIG` target. The store and the results directory are left out of the identity of a
measured tree they sit in, so a run's own output does not mark it dirty. The earlier
`.benchx/provider.json`, `.benchx/config.json` and the `~/.benchx` home directory are gone and not read (only `$BENCHX_HOME` survives, as a store override);
`.benchx/setup.json` (declared environment facts, §3) is a separate file.
`schemas/config/0.1.0/examples` has recommended settings for each harness.

`bx compare` has two modes, chosen by its arguments. Two positional targets
(a path read by the harness, or `@CONFIG[=SOURCE_REF]`, a call to the project's
target provider) mean measure mode; `--run` means read mode; giving
both or neither is an error, as is a flag that belongs to the other mode
(`--baseline` and `--results` are read mode only, since measure mode derives
the baseline from its first target; `--suite`, `--rounds`, `--project`,
`--run-key`, `--out`, `--repetitions`, `--min-time`, `--no-build`, and
`--source-uri` are measure mode only). `--label` names the label in read mode, and carries the two values in
measure mode. §5 describes measure mode.

A source URI given any way (`--source-uri`, a provider's description, the
checkout's `origin`) goes through one normalization before sides are
compared or recorded, and identity does the same: git's SCP-style
`user@host:/abs/path` becomes `ssh://user@host/abs/path`, a relative
`host:path` becomes `ssh://host/~/path` (relative to the login directory, so it
is not mistaken for the absolute `/path`), and a local path becomes a
`file://` URI. Windows drive paths and bracketed IPv6 hosts are recognized.

The CLI is a thin layer: each command is one call into the `benchx` package,
so the workbench, a CI script, or a notebook can do the same without a
subprocess. Measure mode is a call into the session function, which calls the
runner for each order and then the comparator.

The local store has one place per project, `store.parquet` in the project root
unless `paths.store` in `benchx.conf.json` says otherwise; no command takes another.
`$BENCHX_HOME`, when set, relocates the store to `$BENCHX_HOME/store.parquet` and wins over
`paths.store` (for tests, CI and keeping a demo out of your real store).
`bx run` delivers what it measures there: it writes the result files,
then ingests exactly those files. The `runner` module itself stays
store-unaware (`runner.md` §2 principle 6); the command composes it with
ingest, which is the delivery step of `runner.md` §4 and what the workbench
is. The files are still written first, so a result file remains a valid
ingest payload (criterion 1) and a rejected ingest loses nothing.
`--no-ingest` writes the files only, for delivery elsewhere. Delivery is an
invocation choice, never an order field, so replaying an order elsewhere does
not post to the original store.

## 2. The three documents

**Work order.** The prototype reads the work order of `runner-schema.md`
(`schemas/work-order/0.1.0`, `benchx/work-order/0.1.0`) and defines no format
of its own. One order is one side of one round: the runner executes one order
per invocation (`runner.md` §7), and the prototype accepts two target kinds:
`build`, an existing build directory with an optional `source_dir`, and
`working_tree`, a checkout measured as found, which comes from a provider or from
a bare path read by pyperf. The
runner never builds either (`runner.md` §1).

Interleaving needs two order fields. `round` is native to the schema and is
copied to `procedure.round`. `slot`, the realized position across all sides,
is one only whoever coordinates the sides can assign, and the schema rejects
unknown top-level fields, so the prototype validates orders against the schema
extended by exactly one optional integer, `slot`, copied to `procedure.slot`;
nothing else is added, and `slot` is proposed to the schema. The session loop
is the coordinator: `bx compare` in measure mode writes one order per side and
round and runs them alternately (§5). That loop is the workbench's
(`system-decomposition.md` §3.4), since no harness can interleave two separate
binaries (`benchmark-environments.md` §5.7), and it sits above the runner,
which takes one order per invocation.

The smallest ad hoc order, as the session loop writes it:

```json
{
  "schema_version": "benchx/work-order/0.1.0",
  "work_order_id": "urn:uuid:6f1c2b7e-3d4a-4e8b-9a51-0c2d7e9f4b13",
  "state": "resolved",
  "suites": [{"adapter": "google-benchmark",
              "suite": "arrow-compute-vector-selection-benchmark",
              "filter": "TakeChunked.*"}],
  "target": {"kind": "build", "path": "/tmp/bench-hardened",
             "source_dir": "/home/rok/arrow",
             "source": {"uri": "https://github.com/apache/arrow", "type": "git"}},
  "quantities": ["wall-time"],
  "precision": {"repetitions": 10,
                "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.01}},
  "timeouts": {"case_s": 60, "order_s": 600},
  "run_key": "urn:uuid:9b2e4f1a-6c3d-4e8f-a7b0-1c2d3e4f5a6b",
  "requester": {"kind": "workbench", "name": "bx"},
  "plan": [{"id": "p0", "case": "*", "quantity": "wall-time"}],
  "round": 0,
  "slot": 1,
  "labels": {"build": "hardened"}
}
```

The `plan` entry is a placeholder: the runner does not yet cross-check it
against the case list it discovers from the binary (`runner-schema.md` §3.2).

A tracked order adds `project`; `schemas/work-order/0.1.0/examples/arrow-laptop.json`
is the fuller form.

**Result:** exactly the schema §5.2 ingest object, one file per attempt ×
quantity, named by its `ingest_key`. The order is referenced by
`provenance.info.workorder_ref` = `sha256:` over the order's RFC 8785 form;
`runner-schema.md` §5.1 puts the resolved order in `provenance.artifacts`
instead, which the prototype does not yet do. No prototype-only fields, so
criterion 1 holds by construction.

**Comparison document** (output of `bx compare`), following `comparator.md`
§5:

```json
{
  "comparison_version": 1,
  "mode": "run",
  "run_key": "urn:uuid:9b2e4f1a-…",
  "profile": "environments",
  "varying": "provenance.labels.build",
  "baseline": "plain",
  "contender": "hardened",
  "method": {"name": "benchx/paired-relative", "version": "v1",
             "estimator": "median", "k": 3.0, "min_rounds": 3},
  "identity_schema": "benchx/profile/environments/v1+benchx/prototype-identity/v1",
  "local_only": true,
  "units": [
    {"workload": {"name": "TakeChunked…/4194304/2", "parameters": {}},
     "quantity": "wall-time",
     "series": {"baseline": "…", "contender": "…"},
     "rounds": 10,
     "reference_estimate": 0.0455, "effect": 0.024,
     "noise": 0.0021, "noise_source": "paired-rounds",
     "verdict": "regressed", "reason": null,
     "unmatched_differences": ["/coordinates/subject/configuration/options/ARROW_HARDENED"]}
  ],
  "missing": [{"workload": "…", "quantity": "wall-time", "side": "contender"}],
  "failed_invariants": [],
  "exclusions": [],
  "inputs": [{"producer": "benchx/gbench-adapter", "ingest_key": "…"}]
}
```

`--json` emits this; the default output renders it as the reviewer table
(regressed, improved, no change detected, and indeterminate sections, then
the missing, failed-invariant, and excluded listings with counts). In
the `environments` profile, a comparison in which either side is not `clean`
has `local_only: true` (schema §5.5); the `revisions` profile refuses it
instead (§5).

## 3. Runner and adapter (`bx run`)

The runner validates the order against the work-order schema, resolves
`target.path` and the suite name to a binary, and refuses the order if the
binary is absent. It builds nothing and changes nothing on the machine; it
passes its own environment through, with the order's `environment_variables`
applied on top, to the launched process alone (`benchmark-environments.md`
§2.2).

The runner refuses, before running anything, an order it cannot apply
exactly, rather than dropping what it does not understand: another adapter or
target kind, more than one suite, `include` or `exclude`, a `benchmark`
object, `workload_parameters` (Google Benchmark takes none), a quantity other
than `wall-time` or `cpu-time`, or a precision key the adapter has no flag
for. It also refuses each field whose feature is deferred: `environment_policy`
(enforcing launch settings, verifying requested hardware), a declared `build`,
`components`, and `schedule`. A refusal names the field and says it is
deferred. `timeouts.order_s` is required by the schema and not enforced.
Precision keys the order leaves out are recorded with Google Benchmark's
defaults.

The Google Benchmark adapter's driving half lists the planned cases with
`--benchmark_list_tests` and the order's filter, then runs each planned case
in its own process, with `--benchmark_filter=^case$`,
`--benchmark_repetitions`, `--benchmark_min_time`, and JSON output. One
process per case is what lets `timeout_seconds` apply to a single workload
variant, as `timeouts.case_s` requires, and confines a crash to its case. Stdout, stderr,
and the native JSON are kept as provenance artifacts. Its translating half follows
`harness-adapter.md` §6.1: iteration rows become observations, aggregate rows
producer summaries, encoded names and user counters are split only under
`parameter_rules` (none in the prototype, so names stay whole and counters go
to `environment.system`), and a planned case absent from the output is an `error`
result with reason `harness.no-output` (§5 step 5). Keys follow §4.6: one
`attempt_key` per case per invocation, and an `ingest_key` whose attempt
discriminator is the slot, since one run holds an attempt of each variant per
side and round, and the slot is the one number unique to each.

Context comes from the sources `benchmark-environments.md` §3 defines, each
landing where its §3.4 puts it:

- **Snapshot** (§3.1, the part readable without privileges): host name, CPU
  model, and core count as environment identity under `machine/v1`, also in
  ad hoc mode, so both profiles have a full environment identity (schema
  §5.5); OS, kernel, libc, and governor in `environment.system`, and load as a
  condition measurement; an allowlist of thread-control environment variables
  in `environment.system`,
  which is the allowlist of `benchmark-environments.md` §3.1.
  `process.environment` reads differently by adapter. Google Benchmark takes the
  runner's own environment and omits it when no allowlisted variable is set.
  pyperf records what the workers actually had, so it is `{}` when the
  capture worked and none was set, and null only when nothing was captured
  (`env-capture-missing`); null matches nothing, so such a result shares no
  series (schema §4.3). The load average the runner reads before each case is
  a sibling `load-average` result with the case's `attempt_key`, its
  `protocol.probe` naming how it was read, and `measurement.kind` is
  `condition`, so tools leave it out of what they compare and show.
- **Source identity**, from `target.source_dir`, or from the
  `CMAKE_HOME_DIRECTORY` the build directory records when the order names
  none: revision, `subject_dirty`, and `subject_tree` (schema §4.1). When
  neither locates a checkout the runner refuses the order, because a result
  must state its revision (`runner.md` §2), and `runner-schema.md` §5.1 refuses
  an order with no revision to record. The benchmark code is the subject's own
  checkout, so the benchmark revision, dirty flag, and tree are the subject's,
  unless the harness's suite is a source file (pyperf) outside that checkout: then
  the order carries `benchmark`, and the result records the suite file's own
  source, revision (when under git) and blob id as `benchmark_tree`
  (`runner-schema.md` §3.3).
- **Build configuration**, from a built-in plugin that reads
  `CMakeCache.txt` in the build directory: build type, compiler and version,
  flags, and the project's own options (cache entries prefixed with the
  upper-cased project name, `ARROW_*` for Arrow), never paths, so two directories configured
the same way report the same configuration. Nobody declared these, so the detected
  values are recorded as the intended ones in `coordinates.subject.configuration`
  (§3.4). This is what makes the hardened and plain directories differ in
  coordinates, which the `environments` profile requires.
  The pyperf adapter's `python_cflags` is the exception to "never paths": it is
  recorded verbatim in `environment.system.runtime`, as `harness-adapter.md` §6.8
  has it, so the same Python build installed under two prefixes reports different
  flags (conda adds its `-isystem` and `-fdebug-prefix-map` paths) and lands in
  different series.
- **Declared facts**, from `.benchx/setup.json` or `BENCHX_SETUP_FILE`
  (`harness-adapter.md` §5.1), for what the cache cannot tell, such as how a
  dependency was installed. The prototype has no placement rules, so they
  land in `environment.system.setup`, which the default identity policy matches.
- **Context collectors** (`harness-adapter.md` §5.1), named under `collectors` in
  `benchx.conf.json` and copied into the work order. The runner runs each command once,
  before the cases, in the harness's working directory and environment (`{root}` in an
  argument is the project root), under `timeout_s` (default 60). It prints one JSON object.
  The runner saves it whole as an artifact, and puts its `summary` object and its
  sha256 into every result's `environment.system.<name>`. A collector that fails adds no
  facts and gives the results `collector-failed`. Only `before`, once per order, is
  implemented. The commands are executed, so `bx run` refuses an order that has
  collectors unless given `--allow-collectors`; `bx bench` and `bx compare` build the
  order from the user's own configuration and allow them.
- **Verification** of a collector's facts: `verify: "how_built"` requires every key of the
  target's `how_built`, when that is an object (a provider may declare facts there; as a
  string it is text and never checked), to be among the collected facts with exactly the
  same value, at any depth; extra collected keys are fine. `expect` requires facts by
  value. A failed check, or a collector that cannot run, refuses the order before any case
  (`on_fail: refuse`, the default), or with `on_fail: warn` gives every result the
  warning `collector-mismatch`. `prototype/examples/numpy-bench` uses this to check that
  the numpy a provider described is the numpy the workers import.

`benchmark-environments.md` §3.4 asks that every fact carry its source
(`detected`, `declared`, or a plugin name). The prototype does not record it
yet: §9 question 1 leaves the representation open, and nothing in the slice
would read it.

What the prototype does not detect is recorded as absent, never guessed. In
particular a build directory can be stale against its `source_dir`; the
prototype records the tree as it is at run time and does not detect staleness
(`runner.md` §8 question 2).

## 4. Store (`bx ingest` + queries)

The local store is one Parquet file, the project's `paths.store` (§1);
the one other store is the temporary file `bx compare --results` compares
through. There are no side files:
no JSON, no separate entity tables. Each row is one result, the
`benchmark_result` of schema §4.1:

- **Message columns:** the row `measurement-result-parquet.md` (#30) derives
  from the result schema, typed structs and lists with open objects as
  canonical JSON, for queries.
- **Derived columns:** the store project (the result's own, or
  `local/<hostname>` for a thin one), `reported_coordinates_fingerprint`,
  `payload_sha256`, and an attempt signature over the fields schema §5.3
  requires results of one attempt to share.
- **The document:** `document` holds the result in its RFC 8785 canonical
  form. #30's columns cannot reproduce it, since its mapping is not
  byte-preserving (a bare observation and a structured one map alike), so this
  column is the audit copy and what the comparator reads.
- **The work order:** `work_order_ref`, and `work_order` with the order itself
  when it was delivered beside the results, so every result traces to its
  order inside the file (criterion 6).

The other core tables of schema §4.1, `project`, `source`,
`workload_variant`, `quantity`, `environment`, `revision`, `series`, and
`series_point`, are not stored. They are computed from the rows when a query
asks for them. Schema §4.1 already calls series and series points rebuildable
indexes; at prototype scale, rebuilding them on every query is cheaper than
keeping them consistent, and "entities auto-create" (schema §5.4) holds in the
sense that nothing is rejected for being first seen: the first result with a
quantity defines its unit, and later results are checked against it.

A Parquet file cannot be appended to, so an ingest that accepts anything
rewrites the file: the existing rows and the new ones go to a temporary file
beside it, which is synced and renamed over the store. The rename is atomic,
so a reader sees the old store or the new one, never a partial one, and the
acknowledgment means validated and durable. When the result schema has gained
a column or nested field since the file was written, the old rows are carried
over with nulls in it, so one file can hold results written under several
schema versions; a schema that removes or retypes a field is a new message
contract and a new store file.

Ingest follows schema §5.4. Each document is read strictly (duplicate keys,
non-finite numbers, and unsafe integers are rejected, as
`measurement-result-parquet.md` §5 describes, since JSON Schema validation
cannot see them) and validated against the result schema. Entities
auto-create. Rejections carry the §5.4 taxonomy codes: *malformed*,
*idempotency conflict*, *unit conflict*, *identity violation*, and
*attempt conflict*; *quarantined* does not occur, since quarantine is
deferred. Each is reported on stderr with a non-zero exit, and everything is
counted (`ingested 42, duplicate 3, rejected 1 (unit-conflict)`). Re-running
`bx ingest` on the same directory is a visible no-op (criterion 2).

Series need an identity policy and a list of estimators (schema §4.3, §4.5).
The prototype has one of each, in code (`benchx/identity.py`), and keeps them
apart, so adding an estimator never renames the policy:

- **Identity policy** `benchx/prototype-identity/v1` (`benchx/identity.py`):
  the default policy (`schemas/identity-policy/0.1.0/examples/default.json`)
  with the benchmark's revision ignored. A test checks it against the schema
  and pins it to its name, so an edit without a new name fails. The prototype has no declared benchmark versions, and
  with benchmarks in the subject's checkout the benchmark revision changes on
  every commit, so matching it would split history at every point (schema §1).
  Being ignored, its change is flagged on each point where it happens. A thin
  result's identity carries its implicit project.
- **Estimators** (`ESTIMATORS`): `median`, derived from observations, plus
  any producer estimate a result carries.

The identity policy is the default projection, with the subject revision as
its axis. The store can materialize any other projection built in code from
its roles over the same rows; nothing in the CLI asks for one yet.

A thin result gets the implicit project `local/<hostname>` (schema §4.1).
Idempotency, unit, and attempt-integrity checks scan the store's bookkeeping
columns, which is fine at prototype scale; a real deployment would index them.

`bx head` shows the store's size and its latest results, newest first, with
run key, workload, quantity, status, median, revision, project, and labels;
with `--json` it prints the stored documents instead, and with `--table` the `bx bench`
table (median, mean ± sd, range, n, status) of each measurand, led by revision, run key and project;
condition measurements are left out, as in `bx bench`.
`--project` keeps one store project's results and `--workload` those whose benchmark name
contains it; both apply before `-n`. Recency is ingest
order, since every ingest appends to the rewritten file.

`bx series` and `bx history` are queries over the derived `series` and
`series_point`.
`bx history` lists a series' points in axis order, each with the ignored
facts that changed since the point before it. Without a revision graph
revision order is unavailable (schema §4.1), so an axis on the revision lists
them by measurement start and says so. It evaluates nothing, since history
mode is deferred.

## 5. Comparator (`bx compare`)

Run mode as `comparator.md` §2.3 defines it. Inputs always come from a store
(§2.1): the local store by default, or, with `--results DIR`, a throwaway
store the directory of result files is ingested into first, for results that
were never delivered to the local one. The request names the run, the profile, and the baseline value of the
varying coordinate. Each profile is a projection built in code from the
identity policy (`benchx/compare.py`): its axis tells the sides apart, and
every fact the policy matches must agree across both sides.

| Profile | Axis | `--baseline` names | Sides must agree on |
|---|---|---|---|
| `revisions` | subject tree id | a revision or tree id prefix | everything the identity policy matches |
| `environments` | the label `--label` gives, then subject configuration, pinned components, and `environment.system` | the label value | everything else the identity policy matches, and subject revision and tree |

A side is one whole axis value, so one label covering two builds is three
sides, not one. Before any verdict, the eligibility guard (§3) checks that the
matched facts agree (`sides-agree`) and the invariants the profile names over
the run: exactly two sides, which for `environments` differ in more than their
label, equal attempt counts per side, and sides alternating by
`procedure.slot`. Results the order did not ask for are not compared. A failed invariant is recorded and the affected units get no
verdict; under `revisions` a side that is not `clean` fails the `clean-tree`
invariant the same way. A variant present on one side only is listed as missing
(criterion 4). Results with a status other than `success` are excluded and
listed.

Per comparison unit (one workload variant and quantity), the method
`benchx/paired-relative/v1`:

- pairs the two sides' attempts by `procedure.round`;
- takes each attempt's median observation;
- computes per round the relative difference d = contender / baseline − 1;
- reports the effect as the mean of d, and the noise as the standard error
  of d across rounds (`noise_source: paired-rounds`);
- indicates a change when |effect| > *k* × noise, labeled by the quantity's
  direction when known; otherwise *no change detected*;
- is indeterminate with a reason when fewer than `min_rounds` pairs exist
  (`insufficient-rounds`), when a baseline value is zero, or when the guard
  excluded the unit.

Deterministic quantities are deferred: none of the prototype's quantities is
one, so a unit whose quantity is declared deterministic is indeterminate with
reason `deterministic-deferred` rather than compared exactly (comparator §4).
Default
*k* = 3 and `min_rounds` = 3, overridable per invocation; per-project
configuration is deferred. Choosing a paired method for run mode is the
prototype's answer to `comparator.md` §7 question 1, open until reviewed.

Comparison documents are recomputed on demand and not stored
(`comparator.md` §5); the store keeps their inputs.

**Measure mode.** `bx compare BASELINE CONTENDER …` runs both sides and then
compares them; it is the workbench's session loop (`system-decomposition.md`
§3.4) in the prototype's CLI. It:

1. checks that both build directories and the suite binary exist, and locates
   each side's source checkout, from `SOURCE_DIR` or the
   `CMAKE_HOME_DIRECTORY` the build records. Under `revisions` it also checks
   that both trees are clean, and refuses before running anything, so a dirty
   tree fails at once and not after R rounds;
2. fixes a run key, from `--run-key` or a timestamped default;
3. for each of R rounds, for each side in order (baseline, then contender),
   writes one work order with the shared run key, `round`, and a running
   `slot`, and runs it through the same call `bx run` uses, which writes the
   result files and delivers them to the local store;
4. calls the comparator on that run key and profile. The baseline value is
   derived from the first target: its subject tree id for `revisions`, and the
   first value of `--label NAME=BASE,CONTENDER` for `environments`.

It is exactly `bx run` 2R times followed by read-mode `bx compare`; it adds no
document, schema, or contract. It builds nothing itself: both targets must exist
when it looks. A target given as `@CONFIG` is first requested from the
project's target provider (`system-decomposition.md` §3.4), which `prepare`s
it (and may build it) or, with `--no-build`, only `describe`s it; a target
given as a build directory is read from its `.benchx-target.json` sidecar if
it has one. The description's declared text (`how_built`, `activation`,
`shell`, `provider`) is copied into each order, recorded in each result's
`provenance.info`, and shown in the comparison; it is never executed or
checked. If it stops partway, the results already
delivered stay in the store, and a read-mode comparison of the run key reports
missing results or a failed alternation invariant, as UC-03 §10 requires.
Allowing a local-only `revisions` comparison of a dirty tree is open
(`system-decomposition.md` §6 question 4).

## 6. Demonstration

Both profiles use one shape: two prepared build directories, one run key, R
rounds, and `bx compare` in measure mode, which alternates the sides and
assigns `round` and `slot`. The demo script plays the user, and in that role
the target provider: it prepares the sources and build directories with git
and CMake, since benchx builds nothing. `prototype/examples/archery-local`
shows the other route, where a provider script named in
`benchx.conf.json` builds and describes the targets and the sides are
given as `@plain` and `@hardened`.

- **Revisions:** two git worktrees of one source at the base and head
  commits, each built into its own directory with the same configuration.
  The two orders differ only in `target`. Measure and compare with
  `bx compare BASE_BUILD:BASE_SRC HEAD_BUILD:HEAD_SRC --profile revisions --suite … --rounds R`;
  the same run key compares again in read mode with
  `bx compare --run KEY --profile revisions --baseline <base commit>`.
- **Environments:** one checkout configured twice with one option changed,
  as the Arrow local story does for hardening, into `/tmp/bench-plain` and
  `/tmp/bench-hardened`. The orders differ in `target.path` and in the
  label `build: plain|hardened`. Measure and compare with
  `bx compare PLAIN_BUILD:SRC HARDENED_BUILD:SRC --profile environments --label build=plain,hardened --suite … --rounds R`;
  read mode repeats it with
  `bx compare --run KEY --profile environments --label build --baseline plain`.

The demo runs the two shapes once each. Revisions run ad hoc: the orders
name no project, and `bx compare --results` compares them through a
throwaway store. Environments run tracked: the orders name a project, and
the comparison reads the local store. The environments results are then
re-ingested to show idempotency, and compared again with `--results`; that
document must equal the one from the local store (criterion 8).

Every `bx run` delivers to the local store, so both shapes' results land in
the demo project's store (`prototype/examples/demo/store.parquet`), the ad hoc ones as thin results of project
`local/<hostname>`. Run keys carry a timestamp, so successive demo runs add
rows rather than colliding on `ingest_key`. The store and each series'
history grow from run to run, and the demo ends with `bx head`.

## 7. Deliberate simplifications

- Two harnesses (pyperf and Google Benchmark, through `bx bench` and `bx compare`) and two target
  kinds (`build`, `working_tree`). The runner builds nothing, ever: the user
  prepares both sides (`benchmark-environments.md` §2). The prototype applies
  `environment_variables` and defers the rest of the environment policy, and it
  refuses an order that names one rather than ignoring it (§3).
- The target-provider hook is minimal: one provider command per project
  (`provider.command` in `benchx.conf.json`), `describe` and `prepare` only, `build` targets
  only, and a source ref other than `WORKSPACE` is the provider's business.
  How cached builds are named and expired stays open
  (`system-decomposition.md` §6 question 5). The demo prepares its builds by
  hand.
- Environment identity is the snapshot's host name, CPU model, and core
  count; continuity events, quarantine, and artifact storage are absent.
- One identity policy, in code, and two profiles; median, mean, and min are
  the only estimators the prototype can derive.
- The store is one file, rewritten on every ingest that accepts something,
  with no concurrency story beyond the atomic rename; entities and series are
  computed on query, and ingest-time checks are scans, not indexes.
- Staleness of a build directory against its source tree is not detected.
- History has no revision order: there is no revision graph, so `bx history`
  lists points by measurement start.

None of these changes a contract; each is a smaller implementation of one.

## 8. Implementation notes

The prototype is written in **Python**, chosen on 2026-08-31 after an
ecosystem survey against Rust and Go for the most mature libraries on every
contract-critical need and the fastest iteration while the document formats
settle:

- `pyarrow` for the single-file Parquet store and nested list/struct columns;
- `jsonschema` with a `referencing` registry, since the work-order schema
  references the result schema;
- `rfc8785` for canonical JSON in fingerprints and the work-order hash;
- the standard library elsewhere (`hashlib`, `subprocess`, `statistics`).

The package has one module per component behind the CLI: `benchx.core`
(document loading, strict JSON, schema validation, and the rules schemas
cannot express, as proposed in #34), `benchx.runner`, `benchx.adapters.gbench`,
`benchx.store`, and `benchx.compare`. It is distributed with `uv tool install`
or `pipx`, with no single-binary requirement. Whether a graduated version is
rewritten in another language is decided later; the documents and the
Parquet layout, not the code, are the interfaces, which keeps that option
cheap.

## 9. Review checklist

The prototype is correct when the eight criteria in `prototype-scope.md` §3
pass. The demo script performs §6 for both profiles, in both shapes.
