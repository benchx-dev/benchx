# benchx prototype

An implementation of [`docs/design/prototype-design.md`](../docs/design/prototype-design.md):
the slice in [`prototype-scope.md`](../docs/design/prototype-scope.md), from a
work order to a comparison document.

```
bx run <order.json> [--out DIR] [--no-ingest]
                                         # runner + Google Benchmark adapter → result files → local store
bx ingest <dir|file>                     # result files and work orders → the local store
bx series [--workload … --quantity …]    # list series
bx history <series>                      # one series' points (a query)
bx head [-n 10] [--project P] [--workload W] [--json|--table]  # the latest results, newest first
bx compare --run KEY --profile revisions|environments --baseline VALUE
           [--label NAME] [--results DIR] [--k 3] [--json]
                                         # read mode: compare results already measured
bx target describe|prepare TARGET        # a target's description (a provider's answer, or a build directory's sidecar)
bx compare BASELINE CONTENDER --profile revisions|environments
           [--adapter pyperf|google-benchmark]                  # default pyperf
           --suite NAME [--filter REGEX] [--quantity wall-time]
           --rounds R [-e NAME=VALUE ...] [--repetitions N] [--min-time S]
           [--project P] [--label NAME=BASE,CONTENDER]
           [--run-key KEY] [--out DIR] [--no-build] [--k 3] [--json]
                                         # measure mode: run both sides, then compare
bx validate <file>… [--kind KIND]        # every problem in each document, by schema and rules
bx bench TARGET --suite NAME [CASE …]    # the single-target form of bx compare: one order, a table
         [--adapter …] [--filter REGEX] [--quantity …] [-e NAME=VALUE ...]
         [--processes N] [--values N] [--min-time S | --loops N] [--warmups N] [--repetitions N]
         [--project P] [--run-key KEY] [--out DIR] [--no-build] [--source-uri URI]
         [--label NAME=VALUE ... --round N --slot K]   # one side of a session other calls join
         [--case-timeout S] [--no-ingest]
bx bench MODULE.py [FUNCTION …] [--tree DIR] [...]
                                         # shorthand: Python functions under pyperf → results → store → table
bx bench PATH --adapter asv [CASE …] [--filter REGEX] [--tree DIR] [...]
                                          # shorthand: an asv benchmark suite, timed by pyperf
bx config init [--dir DIR] [--adapter …] # write a benchx.conf.json stating a harness's defaults
bx config show                           # what benchx.conf.json and the defaults give, and where each value comes from
```

**One project file.** `benchx.conf.json` in the project root (found by walking up from the
working directory; its directory is the root) says how the project is measured: the
`provider`, the `project` name and `source`, an `adapter` block (`name`, `suite`,
`quantities`, `precision`), `environment_variables`, `timeouts`, `paths` for the store
and the results (by default `store.parquet` and `results/` in the root), and the
comparator's `compare` policy. Every value is the command line's, else the file's, else a
built-in default; a required one (`adapter.name`, `adapter.suite`, the target, and for
`bx compare` the profile and `--rounds`) that has none is a hard failure that names the
flag and the key. `--no-project` makes one run ad hoc whatever the file's `project` says.
The schema is `schemas/config/0.1.0`, whose `examples/` hold recommended settings for
pyperf and for Google Benchmark. The store and the results directory are left out of a
measured tree's identity when they sit inside it. `.benchx/setup.json` (declared
environment facts) is a separate file, written by whoever prepares an environment.

Each target is `BUILD_DIR[:SOURCE_DIR]` or `@CONFIG[=SOURCE_REF]`. Two targets
select measure mode and `--run` selects read mode; mixing them is an error.

`@CONFIG` asks the project's **target provider**, the command named by `provider.command`
in `benchx.conf.json` (it runs with the project root as its working directory). The provider speaks the contract of
`system-decomposition.md` §3.4: a JSON request on stdin, a target description
(`schemas/target-description/0.1.0`) on stdout, build output on stderr, and a
nonzero exit for failure. `prepare`, the default, may build; `describe`
(`--no-build`) only reports what exists. A build directory may also carry its own
description as `.benchx-target.json`. A description names the target and may
declare `how_built` and `activation` text, which are copied into the work order
and into every result's `provenance.info`, shown in the comparison, and never
executed or checked: you activate the environment yourself, and the runner
records the allowlisted variables it inherited beside your declaration.
`examples/archery-local` is a complete walk-through.

`bx bench` is the single-target form of `bx compare`: the same way of naming a target
(`@CONFIG[=SOURCE_REF]`, or a path), the same options, the same code; it writes one
order, runs it, and prints a table, where `bx compare` writes many and compares. A bare
path is read by the harness: a build directory for Google Benchmark
(`bx bench build/ --adapter google-benchmark --suite demo-bench --filter BM_Quiet`), a
working tree for [pyperf](https://pyperf.readthedocs.io), the default
(`bx bench . --suite benchmarks/mod.py bench_a`; uncommitted changes included, recorded
as dirty). Cases are the harness's defaults (pyperf: each `bench_*` or `benchmark_*`
function), the exact names given, or a `--filter` regex. A flag the chosen harness does
not take is refused. `-e` sets environment variables for the workers and lands in the
workload's parameters. Precision resolves as flags, then `adapter.precision` in
`benchx.conf.json` (`bx config init` writes a template), then the harness's defaults, and the
resolved values are written into the order. `--quantity` takes a comma-separated list,
and one run collects them all. The workers run the `python` first on `PATH`, which must have
pyperf.

`bx bench MODULE.py [FUNCTION …]` is the shorthand for the quickest case, timing a few
functions: the module's git checkout is the target, its path the suite, and the
functions are exact case names. `bx run` is the layer underneath: it executes a finished
order, so `bx run results/KEY/orders/order.json` replays any bench run.
`examples/pyperf-bench` is a runnable walk-through.

Environments that cannot share a process, such as two venvs with different numpy versions, are
measured by separate `bx bench` calls that share a `--run-key`; `--label NAME=VALUE`, `--round N`
and `--slot K` place each call in the session, and `bx compare --run KEY --profile environments
--label NAME --baseline VALUE` compares the sides ([UC-04](../docs/use-cases/UC-04-cross-env.md)).
`examples/numpy-venvs` is the worked example, with a shell script that drives it.

`bx bench PATH --adapter asv` runs an [asv](https://asv.readthedocs.io) benchmark suite
unchanged, reading asv's benchmark format as a language and timing it with pyperf;
neither asv nor asv_runner is used. PATH is a directory holding `asv.conf.json` (its
`benchmark_dir` is the suite) or the benchmark directory itself, and its checkout is
the target (`bx bench . --adapter asv --suite benchmarks` is the same order).
`--filter` is asv's `--bench`: searched in the name, and in `name(p0, p1)` for each
parameter combination; a CASE is an exact name, `name(p0, p1)` for a combination. `adapters/asv_runtime.py` discovers
the benchmarks as asv does (names, `params`, `setup`/`teardown`/`setup_cache`, skips,
and asv's `version` hash all agree with asv_runner's; a test checks it). The
benchmarks run in the `python` first on `PATH`, which must import pyperf and the
project you are measuring. Each `time_*` benchmark and each of its parameter
combinations is a case, with the parameters in `workload.parameters` as asv's
`repr()` strings; other benchmark types, and modules that fail to import, are
listed and skipped. Each case is one pyperf run from a scratch directory (an unbuilt
checkout of the project cannot shadow the installed one): the manager runs
`setup_cache` once, and each worker runs the setups, then pyperf's warmups and
values. With setup hooks (a benchmark's own `setup` or a module-level one), every
timed call sees fresh state by default, as in asv_runner >= 0.3.0 (asv#966,
asv_runner#53): one call per loop, or setup re-run between calls outside the
timer. For a fast benchmark that timing is mostly timer overhead, and the result
is flagged `setup-per-call-unbatched`. A benchmark whose calls leave the set-up
state intact can declare `setup_scope = "sample"` (on the function, class or
module, as proposed in asv#1613): setup then runs once per value and pyperf
calibrates loops as usual. The flags are pyperf's
(asv's rounds are `--processes`, repeat `--values`, number `--loops`, sample_time
`--min-time`, and `--warmups`); what they leave out comes from each benchmark's own
`repeat`, `number`, `sample_time`, `warmup_time` and `timeout`, then asv's defaults
(2 processes), and each result records its own case's protocol. A case's
`--case-timeout` covers all its rounds (default 1800 s). The harness in
every result is pyperf. Results also record the installed project's version
(`provenance.info.asv.installed`) and asv's hash of the benchmark's code
(`protocol.asv.benchmark_version`), so editing a benchmark starts a new series.

## What it does

- **Work orders** are the `docs/design/runner-schema.md` schema
  (`schemas/work-order/0.1.0` in the repository), extended by one optional
  field the schema does not itself define, `slot` (`round` is native).
  One order is one side of one round; measure mode alternates them.
- **The runner** (`bx run`) validates the order, refuses what it cannot apply
  exactly, runs each planned case in its own process, writes one result file
  per attempt × quantity, plus a `load-average` condition measurement per
  attempt (schema §4.4), and delivers those files to the local store
  (`--no-ingest` to skip). Each result is valid against the result schema on `main`
  (`schemas/measurement-result/0.1.0`). It records the
  zero-configuration snapshot (`machine/v1` identity, and under
  `environment.system` the architecture, OS, kernel, C library, governor, and the
  allowlisted thread and loader variables, extensible with `$BENCHX_ENV_ALLOWLIST`), the source's revision, dirty state, and tree
  id, the build configuration from `CMakeCache.txt`, and declared facts from
  `.benchx/setup.json`. It changes nothing on the machine and builds nothing,
  and it refuses an order that names a deferred field (`environment_policy`,
  a declared `build`, `components`, `schedule`) instead of ignoring it.
- **The store** is one Parquet file, by default `store.parquet` in the project root
  (`paths.store` in `benchx.conf.json` moves it). It reads documents strictly, validates them, and applies the
  schema §5.4 contract: idempotency and the rejection codes `malformed`,
  `idempotency-conflict`, `unit-conflict`, `identity-violation`, and
  `attempt-conflict`. Each result is one row: the columns #30 derives from the
  schema, the store's derived columns, the canonical document, and the work
  order it references. Entities and series are computed from the rows on
  query, under the identity policy in `benchx/identity.py`: the default of
  `schemas/identity-policy/0.1.0` with the benchmark's revision ignored.
  Every accepting ingest rewrites the file atomically.
- **The session loop** (`benchx/session.py`, `bx compare` in measure mode) is
  the workbench's part: it checks both targets (and, for `revisions`, that both
  trees are clean, before running anything), writes one order per side and
  round, alternates them over the rounds, delivers every result to the local
  store, and hands the run to the comparator. It builds nothing: both build
  directories must already exist.
- **The comparator** (`bx compare --run`) runs in run mode for the `revisions` and
  `environments` profiles, each a projection built in code from the identity
  policy (`benchx/compare.py`): the axis tells the sides apart, everything the
  policy matches must agree, and it checks the profile's invariants (`revisions` also
  requires clean trees, UC-03 §10; `environments` labels a dirty side
  local-only), pairs the
  sides by round, and applies `benchx/paired-relative/v1` (effect = mean
  relative paired difference, noise = its standard error, change when
  |effect| > *k* × noise). `environments` names its sides by the label
  `--label` gives. `--results` compares result files through a throwaway
  store: the ad hoc path. Condition measurements (`measurement.kind:
  condition`) are not compared, and deterministic quantities are deferred.

## Run it

```console
$ uv venv && uv pip install -e '.[test]'   # editable: schemas are read from ../schemas
$ .venv/bin/python -m pytest tests     # 344 tests, fake Google Benchmark binary, no compiler
$ ./demo.sh                             # real builds; needs cmake, a C++ compiler, network once
```

`demo.sh` prints every command before running it, as you would type it
(`bx` for `.venv/bin/python -m benchx.cli`, `$WORK` for its scratch
directory), so it doubles as a walkthrough. It prepares a git source with two
commits and three CMake build directories of `examples/demo-suite` (base,
head, and head with `DEMO_HARDENED=ON`), shows the first work order of each
part, then:

1. **revisions, ad hoc:** `bx compare` in measure mode alternates base and head
   over five rounds with orders that name no project, then read mode compares
   the same run key through a throwaway store;
2. **environments, tracked:** `bx compare` alternates plain and hardened, labeled
   `build: plain|hardened`, compares from the local store, re-ingests
   (no-op), and rejects a unit conflict;
3. checks that the throwaway store gives the same comparison document as the
   persistent one, lists series and history, and ends with `bx head`.

Every `bx run` and every measure-mode `bx compare` delivers to the local store, so each demo run adds its
results, ad hoc and tracked, to `examples/demo/store.parquet` (the demo's project root, set
in its `benchx.conf.json`). Run keys carry a
timestamp, so runs accumulate and each series' history grows. Look at it any
time:

```console
$ (cd examples/demo && ../../.venv/bin/python -m benchx.cli head -n 20)
$ rm examples/demo/store.parquet    # start over
```

Both changes add two to three percent of work. The quiet benchmark flags them
as regressed; the noisy one, with about ten percent spread between
repetitions, does not.

## Layout

```
benchx/
  core/              reading and checking documents; problems are returned, not raised
    loader.py        strict JSON: duplicate keys, non-finite and inexact numbers
    catalog.py       the schemas, by kind and version
    validation.py    which schema a document declares, and whether it fits
    rules.py         what the result and work-order schemas cannot express
    errors.py        every problem, with its schema §5.4 rejection code
    hashing.py       canonical form, hashes, order reference
  snapshot.py        what the runner records (benchmark-environments.md §3)
  adapters/gbench.py Google Benchmark, driving and translating halves
  adapters/pyperf.py pyperf, both halves, for Python functions
  adapters/asv.py    asv's benchmark format, timed by pyperf (bx bench --adapter asv)
  adapters/asv_runtime.py  asv's discovery and setup semantics, standalone, copied beside each driver
  bench.py           bx bench: the MODULE.py and asv shorthands, and the results table
  config.py          benchx.conf.json: finding it, validating it, picking a value, the template
  runner.py          one work order in, results out
  session.py         the session loop: alternate two targets over rounds, then compare
  target.py          target descriptions: the provider call, the sidecar, the declared text
  projection.py      projections: roles plus an axis, the identity policy the default one (schema §4.3)
  identity.py        the identity policy and series points (schema §4.3, §4.5)
  parquet.py         the store's row: #30's columns plus derived ones
  store.py           the single-file Parquet store and the ingest contract (schema §5.4)
  compare.py         run mode over a profile's projection, comparison document (comparator.md)
  cli.py             bx (compare has a read mode and a measure mode)
../schemas/          the result and work-order schemas, read in place ($BENCHX_SCHEMAS overrides)
examples/demo-suite  a two-benchmark Google Benchmark suite for the demo
examples/demo        the demo's project root: its benchx.conf.json, and its persistent store
examples/pyperf-bench  Python functions under `bx bench` (own README)
examples/archery-local  an end-to-end, Arrow-local-shaped example with a target provider (own README)
examples/numpy-venvs  UC-04 with two venvs and two numpy versions, driven by run.sh (own README)
tests/               test_prototype.py: the success criteria and the runner and store
                     rules, on a fake Google Benchmark binary; test_comparator.py: the
                     comparator's invariants and method on hand-built results;
                     test_loader, test_validation, test_rules, test_pipeline,
                     test_validate: core and `bx validate`, on the schema's examples
```
