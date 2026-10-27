# Plan: UC-04 on the design

Oct 27, 2026.

This plan makes UC-04 work with what the design documents already
specify, and adds as little new machinery as possible.

## Goal

Make UC-04 work: compare two environments that differ in one dependency
(or build option), measured by separate `bx bench` calls in separate
environments, joined by a run key.

## What UC-04 needs, and what is missing

UC-04 holds the code, the commit and the machine fixed and varies the
environment: `numpy==1.26.4` against `numpy==2.0` under pandas, or two
builds of one source tree. Two interpreters cannot hold both numpy
versions, so no single invocation can do it. The user prepares both
environments, runs the benchmark in each, alternating round by round
under one session, and then compares.

**What already works.**
- The comparator's `environments` profile pairs the sides' attempts by
  round, and checks that the sides alternate by slot, that attempt counts
  are equal, that everything but the varying coordinate agrees, and that
  the sides differ.
- A label names each side (`provenance.labels`), and the round and slot
  flow from the order into each result's `procedure`.
- `bx compare` in measure mode alternates two targets over rounds under
  one run key. Both sides then run in the one environment `bx` was
  started in, which fits build-option comparisons (the archery example)
  but not a dependency that cannot share a process.

**What is missing.**
1. **A separate runtime environment per side.** Three ways to provide it:
   the runner enters each side's declared `activation`; the workbench
   starts the runner inside each side's activated environment; or the
   user's own script activates each environment and calls `bx` once per
   order. This plan takes the third. It is UC-04's own sketch, and the
   design makes the user responsible for preparing and entering the
   environment, with the alternation a loop in the calling script
   (`benchmark-environments.md` §2.2 principle 1, §5.7). benchx then
   executes nothing it was not asked to.
2. **A way to place a run in a session (gap 1).** The sketch passes a
   label, a session and a round on every call. `bx bench` takes
   `--run-key` (the session) but no label, round or slot, and `bx run`
   takes only an order file. The order builder that `bx compare` uses
   already accepts all three, so the work is exposing them.
3. **A way to tell the sides apart (gap 2).** `sides-differ` refuses a
   comparison in which nothing recorded differs. Two venvs over one
   checkout record the same subject and the same environment: one
   benchmark run from two identical venvs gave identical
   `environment.system` and `subject`, and a numpy difference would be
   invisible in the same way. The declared texts (`how_built`,
   `activation`) travel in `provenance.info`, which is never identity.
   The sides need a recorded, machine-readable dependency fact.
4. **The minimum as the estimator.** UC-04 compares the minimum timing
   per environment. The comparator uses a paired median. This is shared
   with other use cases, and the plan leaves it as a follow-up.

Items 2 and 3 are the two gaps the work packages close (gap 1 and
gap 2). Item 1 is a choice the plan makes, and item 4 is deferred.

## Design decisions

Each decision follows the design documents. The reason is given with it.

1. **Record a declared-vs-detected mismatch; never gate the run on it.**
   - The detected value is recorded, the declaration stays in the work
     order, and the result carries a warning (`benchmark-environments.md`
     §3.4; `benchmark-result-schema.md` §4.2).
   - A contradiction "is recorded as it is and never gates the run"
     (`benchmark-environments.md` §2.2, principle 2). Gating is the
     `verify` tier of an environment policy, per rule
     (`runner-schema.md` §4), and the prototype defers policy.
   - The prototype's collector check (`verify`, `expect`) gives
     `collector-mismatch`. With `on_fail: warn` it records and the run
     proceeds, which matches this decision. Its `refuse` default is a
     documented departure (`prototype-design.md` §3), so the UC-04
     example uses `warn`.
   - So there is no `on_mismatch` setting.

2. **The user's collector describes the environment; benchx hard-codes no
   environment kind.**
   - The design's plugins are "read-only describers that know one build
     system, package manager, or accelerator stack"
     (`benchmark-environments.md` §3.3). A context collector is that
     interface (`harness-adapter.md` §5.1): a command the user names,
     which prints one JSON object of facts.
   - Whatever extracts the facts is the user's: `conda list --json`,
     `pixi list`, `pip freeze`, `numpy.show_config()`, `module list`,
     `docker inspect`. benchx needs to know none of them.
   - The facts land under `environment.system.<collector>`
     (`harness-adapter.md` §5.1), which is in the `environments` axis, so
     a difference makes the sides differ and shows in the comparison.
   - A declaration can be checked against the collected facts: `verify:
     how_built` and `expect` (see decision 1). So the need in gap 2 is
     met with detection, a record in the results and a check, for any
     environment kind, without detection built into benchx.

3. **Collector output stays under `environment.system.<collector>`.**
   - `harness-adapter.md` §5.1 puts collected facts there, with the
     collector's name as their source. Moving one into the subject is a
     placement rule (`benchmark-environments.md` §3.4), which is a
     project decision, not something a collector's output keys decide.

4. **The caller sets slot and round.**
   - Alternation across interpreters "is a loop in the calling script
     ... pass `round` and `slot` through"
     (`benchmark-environments.md` §5.7). `round` is "set by the run
     author" (`runner-schema.md` §3). benchx holds no queue (§5.3) and
     wraps no measurement loop (§2.2, principle 4).
   - The comparator already checks alternation, equal attempt counts and
     that the sides differ (`benchmark-result-schema.md` §5.5), so wrong
     numbering is caught there.
   - So `bx bench` gets `--label`, `--round` and `--slot`, with no lock
     and no slot counter.

5. **Typed declared facts are deferred: `--component`, `--build-option`,
   and the runner's `components` and `build`.**
   - The design has a typed home for them. `components[]` and `build` are
     work-order fields (`runner-schema.md` §3) that land in
     `subject.components` and `subject.configuration`, and the prototype
     refuses both (`DEFERRED` in `runner.py`).
   - UC-04 does not need them. It needs the sides to differ in a recorded
     fact, and a way to check a declaration against what is installed.
     Decision 2 gives both for any environment kind.
   - What they would add: the design's home for dependency versions
     instead of `environment.system`; a declaration on each call, with
     no provider or sidecar; no script for Python packages; and the
     design's own warning names. What they cost: most of the new code
     (a package manifest, a reconcile step, name-only resolution, new
     flags) and two schema decisions.
   - So they are a follow-up. See "Potential follow-ups".

6. **No separate "verified but not identity" category.**
   - Which facts split history is the identity policy's choice, not a
     property of the fact (`benchmark-result-schema.md` §4.3;
     `benchmark-environments.md` §3.4, last paragraph).
   - So keeping a fact out of identity, as numpy-spin tracking needs,
     is a policy question (see follow-ups), not a new kind of fact.

7. **Per-side declared values come from the target description or the
   setup file.**
   - A collector's `verify: how_built` checks the collected facts
     against the `how_built` object of the target description
     (`prototype-design.md` §3). The check compares key by key from the
     top level (`collectors._differences`), so the declared object
     mirrors the structure the collector prints: a collector that prints
     `{"summary": {"numpy": "2.0.2"}}` is checked with
     `{"summary": {"numpy": "2.0.2"}}`.
   - Each side has its own description: a provider configuration per
     side (`bx bench @a`, `bx bench @b`), or a sidecar next to a build
     directory (`system-decomposition.md` §3.4). `expect` lives in the
     config, which both sides share, so it cannot differ per side.
   - The setup file (`.benchx/setup.json`, `BENCHX_SETUP_FILE`) is for
     declared facts when the party that prepares an environment is not
     the one that runs benchmarks in it: in an `environments` run, "each
     environment sets `BENCHX_SETUP_FILE` to its own file"
     (`harness-adapter.md` §5.1). It is implemented. Its facts land
     under `environment.system.setup`, in the axis, and are matched as a
     whole because the prototype has no placement rules.

## What the design already gives UC-04

Each item: the need, the design mechanism, the prototype today.

- **Sides named by an environment label.**
  Work order `labels` → `provenance.labels`.
  Today: `bx compare --label` works; **`bx bench` has no `--label`**.
- **Interleaving over rounds, one session.**
  `run_key`, `round`, `slot`, the caller's loop (§5.7).
  Today: the fields flow to results; **`bx bench` cannot set them**.
- **A machine-readable varying coordinate.**
  The `environments` profile axis: `subject.configuration`,
  `subject.components`, `environment.system` (`compare.py`).
  Today: implemented.
- **Environment facts, detected.**
  The user's collector → `environment.system.<name>`, in the axis.
  Today: implemented (PR #71).
- **A check of a declaration against the collected facts.**
  `verify: how_built` and `expect`; `collector-mismatch` with
  `on_fail: warn`.
  Today: implemented, as a documented departure from the design.
- **Declared per-environment facts.**
  Setup file → `environment.system.setup` (`runner.py:177`), in the axis.
  Today: implemented. Nobody has used it for this.
- **Typed pinned dependencies and declared build configuration.**
  Work-order `components[]` and `build`
  (`benchmark-environments.md` §8).
  Today: refused (`DEFERRED`). Deferred by this plan.
- **Detected CMake options.**
  The runner reads `CMakeCache.txt`.
  Today: implemented (archery example).
- **Invariants: equal rounds, alternation, sides differ.**
  Comparator, schema §5.5.
  Today: implemented.

**Consequence.** Gap 2 needs no new code. Either each venv sets
`BENCHX_SETUP_FILE` to a file that says `{"numpy": "2.0.0"}` (declared),
or the project names a collector that prints the dependency's version
(detected, and checkable). Both land in `environment.system`, which is in
the axis, so `sides-differ` should pass. This follows from reading the
code and has not been run, because `bx bench` cannot pass labels yet; the
tests cover it.

## Design

One rule from `benchmark-environments.md` §3.4 settles how a declaration
and a detection meet:

> A coordinate holds the detected value where one exists. A declared
> value fills it only when nothing can be read back, with a warning. A
> disagreement keeps the detected value, keeps the declaration in the
> order, and adds a warning. No fact gates the run.

Where each fact is declared, detected and stored:

- **Dependency versions and other environment facts** (BLAS vendor,
  install method, image digest).
  Declared in the target description's `how_built` object, or the setup
  file. Detected by the user's collector. Stored in
  `environment.system.<collector>`, or `environment.system.setup` for the
  setup file.
- **Build configuration of a CMake build directory.**
  Detected by the runner's CMake reader. Stored in
  `subject.configuration` (unchanged).
- **`how_built`, `activation`, `shell`, provider.**
  Declared in the target description. Not detected. Stored in
  `provenance.info`, never identity (unchanged).

Warnings: `collector-mismatch` when a check fails and `on_fail` is
`warn`, and `collector-failed` when a collector cannot run. The design's
`requested-value-mismatch` and `build-config-unverified` come with the
typed-facts follow-up.

## Work packages

A and B are independent and give a working UC-04 with declared facts. C
adds detection and verification, with no new code. D is tests.

### A. Place a run in a session (gap 1)

- `cli.py`, `bx bench`: `--label NAME=VALUE` (repeatable), `--round N`,
  `--slot N`. The order builder already takes `labels`, `round_` and
  `slot` (`orders.py:30`), and so does the helper that builds a side's
  order (`session.py:117`). `session.bench_target` and its call to
  `_run_side` pass none of them today, although the compare loop does,
  so thread them through from `_common`. No lock, no counter, no
  `--interleave`.
- When a slot is given, write the order to `orders/order-<slot>.json`,
  as `compare_targets` does. `bx bench` writes `orders/order.json` per
  run key, so the loop's calls would otherwise overwrite each other's
  order file.
- `--label` means different things in different commands: in `bx bench`
  it is `NAME=VALUE` and names the side of one call; in measure-mode
  `bx compare` it is `NAME=BASE,CONTENDER`; in read mode it is the name
  alone. The `--help` text of each says so.
- `bx run` is unchanged.
- Refusals at the command line, before any order is built:
  - `--label`, `--round` or `--slot` without an explicit `--run-key`.
    The default run key is a timestamp per call, so two calls would
    never share a run.
  - `--label` or `--round` without `--slot`. The ingest key gets its
    `:slot-N` suffix only when a slot is set (`runner.py:49`), so two
    sides in one run would share an ingest key, and their differing
    payloads would conflict at ingest.
  - a duplicate label name.

  `--no-ingest` is allowed (nothing then joins the run, which is the
  caller's choice).
- The comparator already refuses badly formed runs: non-alternating
  slots, unequal attempts, sides that do not differ. Improve those
  messages to say what to change ("run the sides in the same order each
  round"). For `sides-differ`, the message says the sides differ only in
  the label, and to describe the environment under test with a collector
  or a setup file per environment. The whole package list is not hashed
  into identity by default: that would start a new series for every
  incidental package change.
- Show warnings in the comparison. The comparator collects the warning
  codes (`collector-mismatch`, `collector-failed`) from each side's
  results and lists them beside the facts that differ. The verdict does
  not change, and the comparison is not marked `local_only`.
  `comparator.md` §5 already lists quality caveats in the output, so
  this needs no design change.
- The caller's loop, as the design says (§5.7), is documented in the
  example. A helper that prints the next slot is a potential follow-up
  (see below).

### B. Declared per-environment facts (gap 2, declared half)

- Document the setup-file route for `environments` runs, with a
  two-venv example. No code.
- The example keeps each setup file outside the tree, or gitignores
  `.benchx/`. An untracked file inside the tree changes the tree id and
  marks the tree dirty (`harness-adapter.md` §5.1).

### C. The collector route (gap 2, detected half)

Documentation and an example. No new code.

- The two-venv demo (see "Demo: two venvs, two numpy versions"):
  `benchx.conf.json` names a collector, `deps`, whose command prints
  `{"summary": {"numpy": "<version>"}}`, read from the interpreter under
  test. The loop of package A runs it once per side.
- Per-side checking: each side's provider configuration returns a
  description whose `how_built` is an object that mirrors the
  collector's output, `{"summary": {"numpy": "2.0.0"}}`. The collector
  has `verify: how_built` and `on_fail: warn`. A fact that
  disagrees gives `collector-mismatch` on every result, the run
  proceeds, and the collected value is what is recorded.
- Document the contract: the command runs once per order, in the
  environment the user entered, outside the timed region. The summary is
  what lands in the results, so it should print the same value for the
  same environment, with no paths, timestamps or unsorted lists
  (`harness-adapter.md` §5.1, last paragraph).
- Document that a check's default `on_fail` is `refuse`, a departure
  recorded in `prototype-design.md` §3 and `harness-adapter.md` §5.1.
  The example sets `warn`. Leave the departure alone; revisit it when
  the environment policy lands.
- The collector output's sha256 stays in identity (`collectors.py:104`);
  that is a follow-up.
- Collectors run commands. `bx bench` and `bx compare` run the project's
  own, and `bx run` needs `--allow-collectors`.

### D. Tests

Case, then expected result.

- `bx bench --label env=a --round 0 --slot 0`, then b/0/1, a/1/2, b/1/3
  → the order carries the fields; results carry `provenance.labels`,
  `procedure.round`, `procedure.slot`.
- `--label env=a` without `--slot` → refused, naming `--slot`.
- `--label`, `--round` or `--slot` without `--run-key` → refused.
- Slots a, a, b, b → fails `alternating-slots`.
- Unequal round counts → fails `equal-attempts`.
- Two sides, setup files differ in `numpy` →
  `bx compare --run S --profile environments --label env --baseline a`
  passes `sides-differ`.
- Same, setup files identical → fails `sides-differ`; the message names
  where the sides can differ.
- A collector whose summary differs between the sides (`numpy`, or
  `blas: mkl` against `blas: openblas`) → `sides-differ` passes, and the
  comparison shows the difference under `environment.system.<name>`.
- Same summary on both sides → fails `sides-differ`.
- `verify: how_built` with a declaration that matches the collected
  summary → no warning on any result.
- `verify: how_built` with `on_fail: warn`, and a declared value that
  disagrees → every result carries `collector-mismatch`, the run
  proceeds, and the collected value is recorded.
- The same check with the default `on_fail` → the order is refused
  before any case.
- Comparison where one side has `collector-mismatch` → the output lists
  the warning beside the differing fact; the verdict and `local_only`
  are unchanged.
- Plain `bx bench` → unchanged.
- End to end, two venvs differing only in a tiny pure-Python package →
  verdicts computed; skipped without `uv`.
- CI runs the numpy-venvs demo (see the demo section).

## Non-Python environments

UC-04 §2 includes build systems, so the plan does not depend on any
one package manager or environment kind. The environment may be a venv,
conda, mamba, pixi, environment modules, Nix or a container. The user
prepares and enters it, and benchx hard-codes none of them:

- **Any environment kind: a collector.** The user-side hook for
  describing an environment is a context collector
  (`harness-adapter.md` §5.1), named in `collectors` in
  `benchx.conf.json`. It is a command that prints one JSON object of
  facts, for example from `conda list --json`, `pixi list`,
  `module list` or `docker inspect`. benchx never needs to know the
  tool.
  - It runs once per order, before the cases, in the harness's working
    directory and environment, so under `conda run -n a` or
    `pixi run -e a` it sees that environment.
  - The full output is kept as an artifact. The results carry
    `environment.system.<name>`: the object's `summary` (if it has one)
    and a sha256 of the full output.
  - `environment.system` is in the `environments` axis, so a difference
    in the summary, such as the BLAS flavour (`mkl` against `openblas`),
    makes the sides differ and shows in the comparison.
  - A collector that prints the whole sorted package list makes any
    package difference count. That is the project's choice, and it
    covers undeclared dependencies for projects that want it.
  - A declaration can be checked against the collected facts
    (`verify`, `expect`; see decision 7).
  - Limits: the facts are not typed `components`. The sha256 of the full
    output is in identity (see the follow-up on collector output), so
    collectors should print the same value for the same environment.
- **Containers, libraries outside Python** (Boost, OpenSSL, BLAS). The
  same collector, or a key in the setup file. A declared image digest in
  the setup file lands under `environment.system.setup`, in the axis, so
  two images differ there.
- **CMake.** Detection exists (`CMakeCache.txt` into
  `subject.configuration`). A declared `build` is part of the
  typed-facts follow-up (the archery README says the prototype refuses
  it today).
- **Single-process comparison** (`bx compare @a @b`, as in
  archery-local). One call runs both sides in the one environment `bx`
  started in. A collector runs in that environment for both sides, and
  cannot tell them apart. One setup file cannot differ between them
  either (its default location is under the source directory, which both
  sides share). So a dependency difference between the two sides of one
  call has no route in this plan, apart from CMake-detected options.
  Declaring `build.options` in each side's target description is the
  route, and it belongs to the typed-facts follow-up. archery-local's
  `deps-old` / `deps-new` case waits for it.

## Design-doc changes

Few documents change, because the plan uses mechanisms the design
already has.

- **`UC-04-cross-env.md` is kept as it is.** Its spellings differ from
  the prototype's. The map from one to the other lives next to the
  two-venv example, in `examples/numpy-venvs/README.md` (see "Demo: two
  venvs, two numpy versions"), which links to the use case.
- **`prototype-design.md`.** Record that `bx bench` takes `--label`,
  `--round` and `--slot`, and describe the collector route for UC-04.

`benchmark-environments.md`, `harness-adapter.md`, `runner-schema.md`,
`comparator.md`, `system-decomposition.md` and the result schema do not
change.

## numpy-spin (benchx_examples PR #1)

This section is informational. The plan makes no change to `benchx_user`
(the local checkout of `benchx_examples`) or to the numpy-spin demos.

The PR has three demos. Only one is UC-04-shaped.

- **Thread settings (`num_threads.sh`).** The same benchmark under three
  run keys, with `OPENBLAS_NUM_THREADS` set to 10, to 1, and unset. It
  runs no comparison. Left as it is; this plan does not change it. The
  acceptance test for packages A and C is the two-venv demo below.
- **Build check (collector).** Gating a run on numpy's build facts.
  Unchanged; works through PR #71's `verify` today. Not UC-04.
- **Revision tracking (`track.sh`).** UC-03-like history across
  revisions. Separate follow-up. It needs the numpy version *out* of
  identity, which is an identity-policy decision, not a fact-placement
  one.

**Threads.** `num_threads.sh` runs the benchmark three times, under
three run keys, and prints `bx head --table`. It sets no label, round or
slot, so nothing joins the three runs into a comparison. The variable is
set in the shell, not with `-e`, so it is the inherited environment of
the measured process and lands in `environment.system.process.environment`
(observed). A variable the runner sets would be a workload parameter
instead (`benchmark-result-schema.md` §4.2). With package A the same
three runs could be a UC-04 comparison, because that field is in the
`environments` axis. The schema's own view of a thread sweep is a
`variants` comparison within one suite, which the prototype does not have
(`compare.py` has `revisions` and `environments` only). The plan changes
none of this. Moving the variable to `protocol.threads` is for *enforced*
caps (`threads.max`, environment policy, deferred). It would not apply to
an inherited one. The flags of package A are additive, so the script
keeps working as it is.

**Tracking.** Keeping the numpy version out of identity is the identity
policy's job, not a property of the fact (design decision 6). The
prototype has one fixed policy (`identity.POLICY_DATA`). Either the
follow-up adds a project-level identity policy (an ignore list in
`benchx.conf.json`), or the collector's sha256 leaves identity so that
numpy's version lives only in the collector's artifact, checked by
`verify`. The second option needs the sha256 change: while the sha256
of the full output is in identity (`collectors.py:104`), any collector
fact that changes splits the series, `verify`-only or not. This is not
part of UC-04. Both options are deferred (see follow-ups).

## Demo: two venvs, two numpy versions

A separate demo, at `prototype/examples/numpy-venvs/`, beside
`pyperf-bench`. It does not touch the numpy-spin demos. It is the
acceptance test for packages A and C: UC-04 as its own sketch has it,
two environments that differ only in numpy, driven by a shell script.

Files:
- `README.md`: what it shows, what to install, how to run it, what to
  expect, and the map from UC-04's spellings to the prototype's (below).
  numpy 1.26.4 has wheels only up to Python 3.12, so the demo asks for
  Python 3.11 or 3.12.
- `versions.env`: `NUMPY_A=1.26.4` and `NUMPY_B=2.0.2`, read by the
  script and by the provider, so the two never disagree.
- `benchx.conf.json`: project name, the pyperf adapter with suite
  `bench_suite.py` and a small precision, `paths` into `out/`, a
  `provider` (`./provider.sh`), and the collector:
  `"collectors": {"deps": {"command": ["python", "{root}/describe_env.py"],
  "verify": "how_built", "on_fail": "warn"}}`.
- `bench_suite.py`: three numpy functions, such as a matrix product, a
  sort and a sum.
- `describe_env.py`: prints `{"summary": {"numpy": numpy.__version__}}`
  and nothing else, with no paths or timestamps.
- `provider.sh`: answers `describe` and `prepare` with the same target
  description and builds nothing. The target is this directory as a
  working tree. `how_built` is an object that mirrors the collector's
  output, `{"summary": {"numpy": "<version>"}}`, with the version for
  configuration `a` or `b` from `versions.env`.
- `run.sh`: the driver, below.
- `.gitignore`: `.venv-*/` and `out/`. Untracked venvs inside the tree
  would mark it dirty, so the comparison would be `local_only`, and
  slow the tree hashing. benchx leaves only the store, the results and
  the `--out` paths out of the tree identity.
- `out/`: generated.

The `python` in the collector command is found on `PATH`, so the script
enters each environment the way a user does, by sourcing the venv's
`activate`, in a subshell for each call. Each call then sees exactly one
environment, and the loop's own shell never holds one. Activation also
resets `VIRTUAL_ENV`, which matters under `uv run` in CI, where it points
at the outer environment. Activate scripts can trip `set -u`, so the
script turns it off around the `source`. benchx executes nothing for any
of this.

`run.sh`:

```bash
#!/usr/bin/env bash
# UC-04 with two venvs: the same code, numpy 1.26.4 against numpy 2.0.2.
set -euo pipefail
cd "$(dirname "$0")"
. ./versions.env
ROUNDS=${ROUNDS:-5}
rm -rf out .venv-a .venv-b; mkdir -p out

# Run a command with one venv activated, in a subshell.
in_env() { local venv=".venv-$1"; shift
           ( set +u; . "$venv/bin/activate"; set -u; "$@" ); }

# 1. The user prepares both environments. benchx builds nothing.
for side in a b; do
  v=NUMPY_${side^^}
  uv venv -q --python "${PYTHON:-3.12}" ".venv-$side"
  uv pip install -q --python ".venv-$side/bin/python" \
      "numpy==${!v}" pyperf -e ../..
done

# 2. The caller's loop: alternate the sides, round by round.
S=numpy-$(date +%s); slot=0
for round in $(seq 0 $((ROUNDS - 1))); do
  for side in a b; do
    v=NUMPY_${side^^}
    in_env "$side" bx bench "@$side" \
        --run-key "$S" --label "env=numpy-${!v}" \
        --round "$round" --slot "$slot" --out "out/$S"
    slot=$((slot + 1))
  done
done

# 3. Compare. The sides differ in the collected numpy version.
in_env a bx compare --run "$S" \
    --profile environments --label env --baseline "numpy-$NUMPY_A" \
    | tee out/compare.txt
grep -q "$NUMPY_B" out/compare.txt   # the differing fact is shown

# 4. The check. Declare version b, run in venv a: the collector disagrees.
in_env a bx bench @b --run-key "$S-mismatch" \
    --label "env=wrong" --round 0 --slot 0 --out "out/$S-mismatch"
grep -rq collector-mismatch "out/$S-mismatch"
```

What it should show. This is the expected behaviour, not an output
captured from a run:
- Step 3 prints the comparison for the two sides, with the differing
  fact (`environment.system.deps.numpy`, `1.26.4` against `2.0.2`) and
  a verdict per benchmark.
- Step 4 prints results that carry `collector-mismatch`. The run
  proceeds, because the collector has `on_fail: warn`, and the recorded
  value is the one the collector read.
- Dropping the `--label` difference, so that both sides use the same
  venv, makes step 3 fail `sides-differ`, with the message that says to
  describe the environment under test.

The script exits nonzero unless the comparison shows the differing fact
and the mismatch run carries `collector-mismatch`, so it can run in CI.
It cannot run until package A has landed, because `bx bench` has no
`--label`, `--round` or `--slot` yet. The CI step below is added in the
same change as the demo, after package A.

**Spelling map.** The demo's README carries the map from UC-04's
spellings (`docs/use-cases/UC-04-cross-env.md`, which is kept as
written) to the prototype's, and links to the use case. `run.sh` above
is written in the prototype's. The map:

- `benchx new-session` (`SESSION=$(...)`) → no command. The session is
  the run key, chosen by the caller: `S=numpy-$(date +%s)`.
- `--session-id "$SESSION"` → `--run-key "$S"`. It lands in
  `provenance.run_key`.
- `benchx run run_benchmark.py` → `bx bench`. With a module and
  functions, `bx bench MODULE.py [FUNCTION ...]`; with a prepared target,
  `bx bench @CONFIG --suite NAME`. `bx run` takes an order file, not a
  script.
- `--env-label numpy-1.26.4` → `--label env=numpy-1.26.4`. The field
  `env_label` is `provenance.labels.env`.
- `--round "$round"` → `--round N` and `--slot K`. UC-04 uses one number
  for two things. The prototype separates the round, which pairs one
  attempt of each side (`procedure.round`), from the slot, the realized
  position in the interleaved sequence (`procedure.slot`). UC-04's
  `interleave_position` is the slot. The caller's loop keeps the slot
  counter.
- `benchx compare-environments --session-id S --baseline A --variant B`
  → `bx compare --run S --profile environments --label env
  --baseline A`. The profile is UC-04's `cross-environment-comparison`.
  There are exactly two sides, so the variant is the other one.
- `.venv-baseline/bin/benchx` → the `bx` of that environment, such as
  `.venv-a/bin/bx`.
- The output table (`benchmark_name`, `env`, `time`, `vs. baseline`) →
  the comparison output: verdict groups with the effect, the noise and
  the rounds, and the facts that differ.
- UC-04's `session_id`, `env_label` and `interleave_position` fields →
  `provenance.run_key`, `provenance.labels.env` and `procedure.slot`.
  The schema adds no fields for a profile
  (`benchmark-result-schema.md` §5.5).

**CI.** The `prototype tests` workflow (`.github/workflows/tests.yml`)
already runs `examples/pyperf-bench/run.sh` and
`examples/archery-local/run.sh` after the tests, on a matrix of ubuntu
and macOS with Python 3.11 and 3.13. The plan adds a step for this demo:

```yaml
- name: Run the numpy-venvs example
  if: matrix.os == 'ubuntu-latest' && matrix.python-version == '3.13'
  shell: bash
  run: >-
    ROUNDS=3 uv run --extra test --python ${{ matrix.python-version }}
    bash examples/numpy-venvs/run.sh
```

- The demo makes its own venvs with `uv venv --python 3.12`, so the
  matrix Python only runs the outer script. Running it in every matrix
  cell would repeat the same work, so the step runs in one cell.
- The step needs network access, for the numpy wheels and pyperf, which
  the runners have. `ROUNDS=3` is the fewest rounds that give a verdict
  (`compare.min_rounds` defaults to 3), and keeps the run short.
- The workflow's `paths` filter already covers `prototype/**`.
- The venvs and `out/` are git-ignored.

## Order of work

Tests (package D) land with the package they test.

1. A, with its tests from D: the flags, the refusals, the order file per
   slot, the comparator's messages and warnings. Then B, with the
   setup-file tests (the two sides differ, the two sides are identical).
   UC-04 works, declared only.
2. C, with its tests from D: the collector tests, the two-venv demo and
   its `run.sh`, and the CI step, all in one change. The demo needs the
   flags of package A, so this follows step 1. UC-04 then works with
   detection and a check.
3. The design-doc changes (`prototype-design.md`).

## Potential follow-ups

Not part of this plan. Build only if the need shows up in practice.

- **Typed declared facts: `--component`, `--build-option`, and the
  runner's `components` and `build`.** Deferred (decision 5). The flags
  would fill the work order's `components[]` and `build.options`
  (`runner-schema.md` §3), which land in `subject.components` and
  `subject.configuration`. A name with no revision means "detect it".
  - Why deferred: UC-04 needs the sides to differ in a recorded fact and
    a check of a declaration. A collector with `verify` gives both, for
    any environment kind.
  - What it adds: the design's home for dependency versions; a
    declaration on each call, with no provider or sidecar; no script for
    Python packages; and the design's warning names with no gating.
  - What it contains:
    - The snapshot reads installed versions with `importlib.metadata`
      (`benchmark-environments.md` §3.1, Runtime) and records the
      inventory as a manifest artifact. That is tied to Python, not to
      the environment manager, so it works the same in a venv, conda,
      mamba or pixi. No manager's own metadata is read.
    - A reconcile step for the rule in the Design section: both equal,
      recorded; both differ, detected recorded with
      `requested-value-mismatch`; only detected, recorded; only
      declared, recorded with `build-config-unverified` (and a sibling,
      `component-unverified`, for components, added to
      `benchmark-environments.md` §3.4).
    - A name-only component is resolved by `bx bench` when it builds the
      order, because the work-order schema requires `revision` and
      `bx bench` writes resolved orders. If nothing can be detected, the
      request is refused with a message that says to give a revision.
      This is an unsatisfiable request, refused visibly
      (`benchmark-environments.md` §2.2, principle 2), not a gate on a
      contradiction. A remote runner would have to fill the version in
      itself and record the completed order as the artifact that results
      cite (`runner-schema.md` §3.2).
    - `source` takes a package URL (`pkg:pypi/numpy`, `type: pypi`) or a
      repository URL; `revision` is the version of a released package
      or a commit id. `runner-schema.md` §3 says so.
    - The flags: when a flag and a config value name the same thing, the
      flag wins (the project's usual order is flag, file, default).
    - `build.options` in each side's target description. It is the route
      for a single-process comparison, and for archery-local's
      `deps-old` / `deps-new` case.
    - An optional `placement` block in `benchx.conf.json` that maps a
      collector name and key, or a setup-file key, to its destination
      with the collector (or `declared`) as source
      (`harness-adapter.md` §5.1 says placement rules exist; the
      prototype has none).
  - Cost: most of the code in the first version of this plan, and two
    schema and documentation decisions.
- **Move the numpy-venvs demo to `benchx_examples`.** That repository
  (checked out locally as `benchx_user/numpy-bench`) holds worked
  examples of using benchx, such as `examples/numpy-spin`, and the
  demo's own audience is a user, not the prototype's tests. Moving it
  would change a few things:
  - It installs benchx from a released version or a git ref, not with
    `-e ../..`.
  - Its README is listed in that repository's README.
  - The CI step in this repository goes. That repository has no CI
    workflow today, so it would need one to keep the demo running.
  - `versions.env` and the provider move with it, and nothing in
    `prototype/` depends on them.
  It is deferred because the demo has to run against the prototype's
  own code while package A is being built, and because it needs
  `benchx_examples` to settle how it pins a benchx version.
- **`bx session next`.** A command that reads the store and prints the
  next `slot` and `round` for a run key and label, for example
  `bx session next --run-key S --label env=a`. It would help when the
  two sides are not launched from one script: a run resumed after a
  crash, or sides started from different shells or CI steps. It is left
  out because the design makes the loop the caller's
  (`benchmark-environments.md` §5.7) and `round` is set by the run
  author (`runner-schema.md` §3). A helper that reads the store also
  needs locking: two callers can race for the same slot, and a crashed
  call leaves a gap that looks like a skipped slot. The comparator
  already catches wrong numbering (`alternating-slots`,
  `equal-attempts`).
- **A label-name check at bench time.** Calls in one run can use
  different label names by mistake, for example `--label env=a` and
  `--label environment=b`. Both calls succeed and ingest. Today the
  comparator catches it, but only at compare time, with "a result has no
  /provenance/labels/env", after all the measuring is done. Two steps,
  smallest first:
  - Make the comparator's message name the label keys it found in the
    run ("found `env`, `environment`").
  - Have `bx bench` warn, not refuse, when its label names differ from
    those already stored for the run key. This reads the store before
    measuring, and cannot see `--no-ingest` runs or another store, so it
    belongs with the session bookkeeping above.
- **A project identity policy.** The prototype has one fixed identity
  policy (`identity.POLICY_DATA`), pinned by a test to its name, so a
  project cannot say which facts must not split its series. The numpy
  tracking demo needs this: numpy's own version is already the axis (the
  subject revision), but the collector records it too, so each revision
  becomes its own series. Options: an ignore list of JSON pointers in
  `benchx.conf.json`, added to the built-in policy, with the identity
  schema name changing with the list; or a separate file following
  `identity-policy/0.1.0`, which is closer to the design. Editing the
  policy starts new series for results already stored. Not UC-04: there
  the dependency version is the thing being compared.
- **A package diff in the comparison.** The sides can differ in
  packages nobody described. If they differ only there, `sides-differ`
  fails. If they also differ in a described fact, the comparison passes
  and attributes the whole difference to it: a hidden confound
  (`benchmark-environments.md` §4, recommendation 1). The comparator
  would list the packages that differ between the two sides' package
  lists ("also differs: scipy 1.13 → 1.14") beside the described fact,
  as a caveat, in the way it already carries differences in ignored
  facts. The list would come from a collector's artifact, or from the
  manifest of the typed-facts follow-up.
  The catch: the list is an artifact file the result references, not
  stored in the store (`comparator.md` §2.1 takes its inputs from the
  store). The diff works only when the artifact files are reachable, for
  example in the same results directory, and the comparison must say
  when they were not. A name-to-version map in `provenance.info` would
  be in the store, but adds hundreds of entries to every result.
- **Collector output in identity.** The runner stores each collector's
  `summary` and a sha256 of its full output in
  `environment.system.<name>` (`collectors.py:104`), and the default
  policy matches both. Any collector fact that changes splits the
  series, even one used only for `verify`. Dropping the sha256 from
  identity (the artifact already records it) would let such facts live
  in the artifact alone. It is smaller than the policy item and removes
  the numpy tracking split. It changes PR #71's design, so its author
  should agree.
- **The minimum as the run-mode estimator.** UC-04 §7 compares the
  minimum timing per environment. The comparator compares the median of
  each result's observations per round, takes the mean of the relative
  differences across rounds, and calls a change when the effect exceeds
  `k` times its standard error (`compare.py`, `_unit`). On noisy
  machines the two can disagree, so the comparator may say "no change
  detected" where UC-04's example shows `+9.2%`. The plan keeps the
  comparator as it is, and UC-04 §7 is left as written. An optional
  `--estimator min` would take the minimum of the observations per
  round instead. It needs a new method version, and
  the comparison document's `estimator` field, hard-coded to the median,
  would change. UC-02 wants the minimum too, and the choice of run-mode
  method is open in `comparator.md` §7 question 1, so it is best settled
  there. A table of one number per environment, as in UC-04's example,
  is a smaller rendering-only addition.
- **Retiring the setup file.** If the typed-facts follow-up is built,
  the setup file carries the same kind of fact through a second
  channel. Retiring it would mean removing
  `snapshot.setup_facts`, the `system["setup"]` assignment in
  `runner.py`, `BENCHX_SETUP_FILE`, `test_setup_file`, and the
  setup-file mentions in `prototype/README.md` and `prototype-design.md`.
  `--component`, `--build-option` and the description's `build` would
  replace it. The costs: the design keeps the setup file for the case
  where the party that prepares an environment is not the one that runs
  benchmarks (`harness-adapter.md` §5.1). Without it, the preparer's
  facts reach benchx through a sidecar next to a build directory, or
  through flags the caller's script passes, and two venvs over one
  shared working tree have only the flags. It is a design change
  (`harness-adapter.md` §5.1 and `benchmark-environments.md` §3.2). And
  results already stored keep `environment.system.setup`, while new ones
  would not, so those series would start fresh.
- **Peak memory.** UC-04 §6 lists peak memory (memray) as optional. No
  adapter measures it yet, so it is out of scope here.

## Open questions

None. What was left open is under "Potential follow-ups".
