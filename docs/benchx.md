# BenchX

BenchX is a powerful and flexible benchmarking framework which aims to address multiple
usage scenarios, from running one-off in-development benchmarks to tracking performance
over history of large projects.

The project is grounded in [user stories](https://benchx.dev/user_stories/)
collected from experience of multiple projects. We aim to address various pain points
we faced and worked around while working on Apache Arrow, NumPy, SciPy, Pandas
and related projects, so that `benchx` becomes the go-to solution for performance benchmarking
across the community.

We welcome contributions of all sorts: add a user story, tell us about your use case, try
`benchx` on your workflow/project, open an issue if you found a rough edge,
implement a missing feature.

- Documentation: <https://benchx.dev/>
- Live deployment (Apache Arrow C++): <https://arrow.benchx.dev/>
- Examples & deployments: <https://benchx.dev/examples/>
- Issue tracker: <https://github.com/benchx-dev/benchx/issues/>
- Worked examples repo: <https://github.com/benchx-dev/benchx_examples>
- Reusable CI workbench (GitHub Actions + AWS EC2/S3): <https://github.com/benchx-dev/benchx-ci>
- Apache Arrow tracking repository: <https://github.com/benchx-dev/benchx-arrow>


# Install

Install from [PyPI](https://pypi.org/project/benchx/):

```console
$ pip install benchx                # JSONL store, compare, validate, and bx view (no PyArrow required)
$ pip install 'benchx[parquet]'     # adds PyArrow for the Parquet store and CI --runs views
```


# CLI API

At a glance:


```
# Execute benchmarks:

bx bench TARGET --suite NAME [CASE …]    # the single-target form of bx compare: one order, a table
         [--adapter …] [--filter REGEX] [--quantity …] [-e NAME=VALUE ...]
         [--processes N] [--values N] [--min-time S | --loops N] [--warmups N] [--repetitions N]
         [--project P] [--run-key KEY] [--out DIR] [--no-build] [--source-uri URI]
         [--label NAME=VALUE ... --round R[/N] --slot K]   # one side of a session other calls join
         [--case-timeout S] [--no-ingest]
bx bench MODULE.py [FUNCTION …] [--tree DIR] [...]
                                         # shorthand: Python functions under pyperf → results → store → table
bx bench PATH --adapter asv [CASE …] [--filter REGEX] [--tree DIR] [...]
                                          # shorthand: an asv benchmark suite, timed by pyperf

# Comparisons:

# measure mode: run both sides, then compare
bx compare BASELINE CONTENDER --profile revisions|environments
           [--adapter pyperf|google-benchmark]                  # default pyperf
           --suite NAME [--filter REGEX] [--quantity wall-time]
           [--rounds 6] [-e NAME=VALUE ...] [--repetitions N] [--min-time S]
           [--project P] [--label NAME=BASE,CONTENDER]
           [--run-key KEY] [--out DIR] [--no-build] [--no-ingest] [--k 3] [--min-rounds 4] [--json | --tables [--all]]

# measure mode, one target: alternate the two values of a benchmark parameter
bx compare TARGET --profile variants --adapter pyperf|asv --suite NAME [--rounds 6]
           [--vary NAME] [--baseline VALUE] [--filter REGEX] [...]  # the options above, except --label

# the shorthand of bx bench MODULE.py, in measure mode: Python files, no --suite, pyperf
bx compare BASE/MODULE.py HEAD/MODULE.py --profile revisions|environments --rounds R [...]
bx compare MODULE.py --profile variants --rounds R [--vary NAME] [...]

# read mode: compare results already measured
bx compare --run KEY --profile revisions|environments|variants --baseline VALUE
           [--label NAME] [--vary NAME] [--results DIR] [--k 3] [--json | --tables [--all]]

# low-level command: run a handwritten work oreder
bx run <order.json> [--out DIR] [--no-ingest]
                                         # runner + Google Benchmark adapter → result files → local store

bx ingest <dir|file>                     # result files and work orders → the local store
bx series [--workload … --quantity …]    # list series
bx history <series>                      # one series' points (a query); dirty ones are marked
bx head [-n 10] [--project P] [--workload W] [--json|--table]  # the latest results, newest first; dirty ones are marked
bx view [--out DIR] [--serve] [--host 127.0.0.1] [--port 8000] [--open]
        [--store PATH | --results PATH… | --runs DIR]
        [--project-file PATH] [--title TITLE] [--repo DIR]
        [--views-format json|parquet] [--marts | --no-marts] [--stamp STR]
                                         # build and serve the static benchmark dashboard (no --out: temp dir + serve)

bx target describe|prepare TARGET        # a target's description (a provider's answer, or a build directory's sidecar)
bx validate <file>… [--kind KIND]        # every problem in each document, by schema and rules
bx config init [--dir DIR] [--adapter …] # write a benchx.conf.json stating a harness's defaults
bx config show                           # what benchx.conf.json and the defaults give, and where each value comes from

# every command also takes: -v/-q (what the terminal shows), --log-level LEVEL,
# and --no-log | --log | --log-file PATH (where the log goes; the default is bx.log in the run's folder)
```

# Technical details


**One project file.** `benchx.conf.json` in the project root (found by walking up from the
working directory; its directory is the root) says how the project is measured: the
`provider`, the `project` name and `source`, an `adapter` block (`name`, `suite`,
`quantities`, `precision`), `environment_variables`, `timeouts`, `paths` for the store
and the results (by default `store.parquet` and `results/` in the root), the
comparator's `compare` policy, and the log (`logging`, `log_level`: see "The log" below). Every value is the command line's, else the file's, else a
built-in default; a required one (`adapter.name`, `adapter.suite`, the target, and for
`bx compare` the profile) that has none is a hard failure that names the
flag and the key. `--rounds` defaults to 6 and `--min-rounds` to 4: with fewer paired rounds the
k = 3 threshold flags too many no-op changes (#126). `--no-project` makes one run ad hoc whatever the file's `project` says.
The schema is `schemas/config/0.1.0`, whose `examples/` hold recommended settings for
pyperf and for Google Benchmark. The store and the results directory are left out of a
measured tree's identity when they sit inside it. A tree is dirty when a tracked file differs
from HEAD; untracked files are not part of its identity, and the log names them (INFO, the
first ten). `.benchx/setup.json` (declared
environment facts) is a separate file, written by whoever prepares an environment.

**The log.** `bx bench` and `bx compare` write `bx.log` in the run's output folder
(`--out`, else `RESULTS/RUN_KEY`), and `bx run --out DIR` writes `DIR/bx.log`. Each
invocation appends a header (the benchx version, its pid, the working directory and the
command line as given), the config file it found, the settings it resolved with where each
came from (flag, environment variable, file or default), and then what it decided, and ends
with a footer: `end: ok (exit 0), 41 s`. The word is `ok`, `invalid` (exit 1), `refused`
(exit 2), `crashed` or `interrupted` (Ctrl-C); a log that ends without a footer is the
trace of a process that was killed (`SIGTERM`, `SIGHUP` or `SIGKILL`). A refusal for a bad
flag or value leaves no folder; a run refused later, by the session, leaves `RUN_KEY/`
holding only `bx.log`, which says why. At the default
verbosity the terminal shows what `bx` printed before there was a log: the log is the full record, and `-v` shows its
INFO records on the terminal (`-vv` DEBUG too), `-q` only warnings and errors, `-qq` only
errors (`$BENCHX_VERBOSE` sets it: -2 or more). A `bx bench` call starts with a line saying what
it measures and where it sits (`measuring np_mean.py on @numpy (round 1/5, slot 3)`), and `bx run`
with one naming the order; `-q` keeps it, `-qq` does not. What `bx bench` and `bx run` report on
stdout when they finish, the table of results, the cases that were not measured and the result
line, is in the log as well, a line a record, and `-q` and `-qq` hide it by tiers: `-q` leaves
the not-measured section and the result line (`N result(s) -> DIR`), `-qq` only the
not-measured section (a failed case does not change the exit status, so it must not vanish; a case that failed
without a message is counted on a line that both keep, and a case the harness skipped is expected, so what it
said goes with the table). A
run that measured everything is silent at `-qq`. The ingest counts that follow a run
(`STORE: ingested 12, duplicate 0`) are a progress message on stderr, like `precision:`: shown
by default and with `-v`, not with `-q`. `bx ingest`, `bx compare`, the queries and everything
`--json` prints are answers, shown at every level. A target provider's own stderr, such as a build's output, is the provider's: `-qq`
does not silence it. Each `-v` past `-vv` also asks the harness itself to say more, as `--harness-verbosity N` does: `-vvv` is `-vv` and 1, `-vvvv` is `-vv` and 2. pyperf and asv are then given `--verbose`, and Google Benchmark `--v=N`; the output is logged in full (up to 2000 lines a stream, 40 otherwise). The default, 0, asks for nothing more. The harness prints between values, outside the timed call, but it is output on the machine between samples, so the order records it (`harness_verbosity`, not part of the measurement's identity) and so does each result's `provenance.info`. `$BENCHX_HARNESS_VERBOSITY` and `adapter.verbosity` in the config file set it too, below the flag. The log's own level is `--log-level`
(`debug`, the default, `info`, `warning`, `error`; `$BENCHX_LOG_LEVEL`, or `log_level` in the
file). `--no-log` turns it off, `--log` sends it to stderr instead, `--log-file PATH` appends it
to a file (`{out}` is the run's output folder, `{root}` the project root); the file's key
`logging` takes `false`, `true` or such a path, and is honoured by the commands that have an
output folder, while a flag works for any command. A file named `*.json` is refused, since
`bx ingest` reads such files in a directory. **A `bx.log` records the command line as given,
`-e` values and credentials in a `--source-uri` included, and a command's output as it was
printed. It does not claim to hide secrets: like `workorder-….json` in the same folder, it can
contain them, and a results folder that is archived or shared carries it.** It never holds the environment `bx` inherited, only names of variables, with one exception: the variables benchx itself reads (`BENCHX_HOME`, `BENCHX_VERBOSE`, `BENCHX_LOG_LEVEL`, `BENCHX_HARNESS_VERBOSITY`, `BENCHX_SETUP_FILE`, `BENCHX_ENV_ALLOWLIST`, `BENCHX_SCHEMAS`) are logged with their values, since they are settings someone typed for benchx and decide what it does.

Each target is `BUILD_DIR[:SOURCE_DIR]` or `@CONFIG[=SOURCE_REF]`. Two targets (one for
`--profile variants`) select measure mode and `--run` selects read mode; mixing them is an error.

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
`examples/arrow/archery-local` in [benchx_examples](https://github.com/benchx-dev/benchx_examples)
is a complete walk-through.

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

A function has a version, as an asv benchmark does: its `version` attribute, else a hash of
its source (as asv_runner hashes a plain function: comments and docstrings count; what it
calls, its `params` and the library under test do not). Each result records it in
`protocol.pyperf.benchmark_version`, and protocol is what the two sides of a comparison must
agree on. As `asv compare` does, a function whose version differs between the two revisions,
or during the run, is listed as not comparable (reason `benchmark-changed`) and the other
functions are compared. That suits a function that exercises code elsewhere in the tree.
When the function body is what changes, declare a `version` (`bench_a.version = "1"`). A
function without readable source has no version.

`bx bench MODULE.py [FUNCTION …]` is the shorthand for the quickest case, timing a few
functions: the module's git checkout is the target, its path the suite, and the
functions are exact case names. `bx compare` takes the same shorthand for each target when
there is no `--suite`: every target is then a Python file, and the files have the same path
in their checkouts, since the suite is one name for all the sides. `bx run` is the layer underneath: it executes a finished
order, so `bx run results/KEY/orders/order.json` replays any bench run.
[`examples/pyperf-suite`](examples/pyperf-suite.md) is a runnable walk-through.

Environments that cannot share a process, such as two venvs with different numpy versions, are
measured by separate `bx bench` calls that share a `--run-key`; `--label NAME=VALUE`, `--round R[/N]`
and `--slot K` place each call in the session (`--round 2/5` also says the session has 5 rounds, numbered 0 to 4, which the start line shows), and `bx compare --run KEY --profile environments
--label NAME --baseline VALUE` compares the sides ([UC-04](https://benchx.dev/use-cases/UC-04-cross-env/)).
`examples/numpy-venvs` in [benchx_examples](https://github.com/benchx-dev/benchx_examples)
is the worked example, with a shell script that drives it.

Two functions of one benchmark ([UC-02](https://benchx.dev/use-cases/UC-02-variants/)) are the two values of
one parameter, declared as pyperf and asv benchmarks declare theirs. Build the data once, outside the
function (at module level, or behind `functools.cache`), because pyperf times the whole body:

```python
IMPLS = {"baseline": pandas_native_truncate, "variant": pandas_via_narwhals_truncate}

def bench_truncate(role):
    IMPLS[role](data())

bench_truncate.params = ["baseline", "variant"]
bench_truncate.param_names = ["role"]
```

One command alternates the two values over the rounds and compares them (`adapter.name` in
`benchx.conf.json` makes `--adapter` optional):

```console
$ bx compare . --profile variants --adapter pyperf --suite bench_suite.py
```

The values are recorded as `repr()` strings. The baseline is the first declared value, and
`--baseline "'variant'"` swaps the sides (spelled as recorded, quotes included). `--vary NAME`
names the parameter and is needed only when the selected benchmarks have more than one: every
combination of the others is then its own unit (`bench_truncate [size=1000]`), and `--filter`
narrows the benchmarks. A benchmark without the parameter runs on neither side, and a failed
attempt on either side makes its unit indeterminate (`side-failed`). The verdict is the
comparator's noise-aware one; there is no tolerance flag yet. As for UC-04, the loop can be the
caller's: one `bx bench` call per value (a case name such as `"bench_truncate('baseline')"`, or
`--filter "'baseline'"`) with `--run-key`, `--round` and `--slot`, then `bx compare --run KEY
--profile variants --baseline "'baseline'"`. One `bx bench` call that runs both values is not an
alternating run, and the comparison says so.

`bx bench PATH --adapter asv` runs an [asv](https://asv.readthedocs.io) benchmark suite
unchanged, reading asv's benchmark format as a language and timing it with pyperf;
neither asv nor asv_runner is used. PATH is a directory holding `asv.conf.json` (its
`benchmark_dir` is the suite) or the benchmark directory itself, and its checkout is
the target (`bx bench . --adapter asv --suite benchmarks` is the same order).
`--filter` is asv's `--bench`: searched in the name, and in `name(p0, p1)` for each
parameter combination, the arguments in the order of `param_names`, each as its `repr`; a CASE is an exact name,
`name(p0, p1)` for a combination. A filter that names a parametrized benchmark and none of its combinations is
refused with that order and one case as it is searched. The runner records the order in
`provenance.info.parameter_names` (the store keeps a document with its keys sorted), and `bx bench`, `bx head --table`
and `bx compare` show the parameters in it, so what they print can be given to `--filter`; results made before have
no such list and show them sorted. `adapters/asv_runtime.py` discovers
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
(`protocol.asv.benchmark_version`), so editing a benchmark starts a new series. An asv
suite outside the subject's checkout is refused (an asv suite is a directory, and a suite
outside the checkout is identified by one file): put it inside the checkout.

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
- **The store** is one Parquet file (`store.parquet`, when `pyarrow` / `benchx[parquet]` is
  installed) or one JSONL file (`store.jsonl`, using only the standard library) in the project root
  (`paths.store` in `benchx.conf.json` moves or selects the format). Both backends read documents
  strictly, validate them, and apply the same schema §5.4 contract: idempotency and the rejection
  codes `malformed`, `idempotency-conflict`, `unit-conflict`, `identity-violation`, and
  `attempt-conflict`. Each result is one row: the canonical document, the work order it references,
  and the store's derived columns (plus the typed message columns of
  [`measurement-result-parquet.md`](design/measurement-result-parquet.md) in Parquet). Entities
  and series are computed from the rows on query, under the identity policy in `benchx/identity.py`:
  the default of `schemas/identity-policy/0.1.0` with the benchmark's revision ignored. Every
  accepting ingest rewrites the file atomically.
- **Schema validation** checks the shipped schemas' `date-time` and `uri` formats directly; custom
  `$BENCHX_SCHEMAS` using other JSON Schema formats require the corresponding `jsonschema` format
  validator.
- **The session loop** (`benchx/session.py`, `bx compare` in measure mode) is
  the workbench's part: it checks both targets (and, for `revisions`, that both
  trees have a working-tree id, before running anything; it says which sides
  are not clean), writes one order per side and
  round, alternates them over the rounds with the order swapped every other round
  (baseline first, then contender first, and so on, so that a drift that favours
  whichever side runs first cancels rather than biasing the effect), delivers every result to the local
  store, and hands the run to the comparator. It builds nothing: both build
  directories must already exist. For `variants` it has one target: it lists
  the cases once, splits them by the two values of one parameter, and writes
  one order per value and round.
- **The comparator** (`bx compare --run`) runs in run mode for the `revisions`,
  `environments` and `variants` profiles, each a projection built in code from the identity
  policy (`benchx/compare.py`): the axis tells the sides apart, everything the
  policy matches must agree, and it checks the profile's invariants (`revisions` also
  requires one tree id per side and, for a benchmark recorded apart from the
  subject, one benchmark file throughout, UC-03 §10; a dirty side makes any
  profile's comparison local-only), pairs the
  sides by round, and applies `benchx/paired-relative/v2` (effect = mean
  relative paired difference, noise = its standard error, change when
  |effect| exceeds noise times the t quantile, at the rounds' degrees of freedom, with
  the two-sided tail of a normal at *k* σ; `--k 3` is a 0.27% false-alarm rate per unit
  whatever the number of rounds). `environments` names its sides by the label
  `--label` gives, and `variants` by the two values of one workload parameter
  (`--vary`); under `variants` a failed attempt on either side fails its unit
  (`side-failed`). `--results` reads and validates standalone JSON result files on disk without
  ingesting them into the local store: the ad hoc path. Condition measurements (`measurement.kind:
  condition`) are not compared, and deterministic quantities are deferred.
- **The tables** (`--tables`, `benchx/tables.py`) print, after the usual summary, what
  `asv continuous` prints, laid out and rounded as asv does: a table of each side (a value
  `4.71±0.08μs` per parameter combination, every cell centered), then one of the changes
  (Change, Before, After, Ratio, Noise, Verdict, Benchmark), only the changed rows unless `--all`.
  They are recomputed for display from the run's results; the comparison document is the
  same. A side's value is the median of its per-round medians and its `±` their standard
  deviation (not asv's `±`); `Change` and `Verdict` are the comparator's verdict (`+`
  regressed, `-` improved), `Noise` the one it compares the effect with (a change is more than *k*
  times it), and `Ratio` is After over Before. A text view: not with `--json`.
- **Views** (`bx view`, `benchx/views/`) derive flat tables (`commits`, `points`, `changes`,
  `latest`, `movers`) and static view payloads in pure Python from the local store (`store.jsonl`
  or `store.parquet`), JSON result files (`--results`, no PyArrow required), or a CI `--runs`
  directory (per-run `runs/<sha>/<node>/{run,results}.parquet` or monthly `runs.parquet` +
  `results/*.parquet`). By default (`--views-format json`, no PyArrow required) it writes JSON
  payloads (`landing`, `series`, `sparks`, `history/<suite>`, `commits/<sha>`); with
  `--views-format parquet` (requires `benchx[parquet]`) it writes `landing.json` plus Snappy
  Parquet payloads (`series.parquet`, `commits.parquet`, `commit_nodes.parquet`, `changes.parquet`,
  `failures.parquet`, `history/<suite>.parquet`) that the dashboard reads directly via `hyparquet`
  and HTTP byte-range requests. With no `--out`, `bx view` builds into a temporary directory and
  serves the bundled static dashboard over local HTTP (with HTTP `Range` support) until Ctrl-C;
  with `--out DIR`, it writes a deployable static site (and optional Parquet marts under `data/`
  when `pyarrow` is installed, unless `--no-marts`). Series are keyed by `(suite, case, params,
  quantity, node)` for per-commit revision history; cross-environment comparisons on the same
  commit and host are inspected via `bx compare --profile environments`.

**Stopping a run.** Ctrl-C stops the benchmarks `bx` started, within about a second; pyperf gets the Ctrl-C and stops its worker.
A second Ctrl-C does not wait. Results already stored
stay, and the case that was running writes nothing. Only Ctrl-C is handled: `SIGTERM`, a closed terminal
(`SIGHUP`) and `SIGKILL` end `bx` at once, as they end pyperf, and a pyperf worker may then keep running until its
current process ends.

## Run it

```console
$ uv venv && uv pip install -e '.[parquet,test]'   # editable; omit `parquet` for a minimal Arrow-free install
$ .venv/bin/python -m pytest tests                 # fake Google Benchmark binary, no compiler
```

`tests/test_e2e_numpy_revisions.py` is an end-to-end test that builds NumPy with
`spin` and compares two tags in one `bx compare`. It is skipped unless
`BENCHX_E2E_NUMPY` (a numpy git checkout) and `BENCHX_E2E_EXAMPLE` (the
`numpy-revisions` example directory) are set and `spin` is on the PATH; it also
needs the project-file entries of PR #65 and PR #71. `BENCHX_E2E_WORKTREES`
keeps the builds between runs, `BENCHX_E2E_REVISIONS` picks the two tags,
`BENCHX_E2E_SPIN_EXAMPLE` (the `numpy-spin` example directory) adds a single-run
scenario, and the test's docstring has the rest.

A regular (non-editable) install ships the test suite too, so an installed copy can test itself:
`pip install 'benchx[parquet,test]'` (or `pip install 'benchx[test]'` for the Arrow-free subset)
followed by `python -m pytest --pyargs benchx`.

The end-to-end examples that build real software are in
[benchx_examples](https://github.com/benchx-dev/benchx_examples), and the `e2e`
workflow runs them. `examples/arrow/demo/run.sh` there is a walkthrough of both comparison
profiles on a small Google Benchmark suite, with real CMake builds (it needs cmake, a
C++ compiler, and network access once). It prints every command before running it,
as you would type it, so it doubles as a walkthrough.

