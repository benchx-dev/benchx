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
bx head [-n 10] [--json]                 # the latest results in the store, newest first
bx compare --run KEY --profile revisions|environments --baseline VALUE
           [--label NAME] [--results DIR] [--k 3] [--json]
bx validate <file>… [--kind measurement-result|work-order|comparison-document]
                                         # every problem in each document, by schema and rules
```

## What it does

- **Work orders** are the #35 schema (`schemas/work-order/0.1.0` in the
  repository), extended by the optional `round` and `slot` #35 lists as open.
  One order is one side of one round; the calling script alternates them.
- **The runner** (`bx run`) validates the order, refuses what it cannot apply
  exactly, runs each planned case in its own process, writes one result file
  per attempt × quantity, and delivers those files to the local store
  (`--no-ingest` to skip). Each result is valid against the result schema on `main`
  (`schemas/measurement-result/0.1.0`). It records the
  zero-configuration snapshot (`machine/v1` identity, OS, kernel, load,
  allowlisted thread variables), the source's revision, dirty state, and tree
  id, the build configuration from `CMakeCache.txt`, and declared facts from
  `.benchx/setup.json`. It changes nothing on the machine and builds nothing.
- **The store** is one Parquet file, always `~/.benchx/store.parquet`
  (`BENCHX_HOME=...` relocates benchx's home, as the tests do). It reads documents strictly, validates them, and applies the
  schema §5.4 contract: idempotency and the rejection codes `malformed`,
  `idempotency-conflict`, `unit-conflict`, `identity-violation`, and
  `attempt-conflict`. Each result is one row: the columns #30 derives from the
  schema, the store's derived columns, the canonical document, and the work
  order it references. Entities and series are computed from the rows on
  query. Every accepting ingest rewrites the file atomically.
- **The comparator** (`bx compare`) runs in run mode for the `revisions` and
  `environments` profiles: it checks the profile's invariants, pairs the
  sides by round, and applies `benchx/paired-relative/v1` (effect = mean
  relative paired difference, noise = its standard error, change when
  |effect| > *k* × noise). `environments` names its sides by the label
  `--label` gives. `--results` compares result files through a throwaway
  store: the ad hoc path. Deterministic quantities are deferred.

## Run it

```console
$ uv venv && uv pip install -e '.[test]'   # editable: schemas are read from ../schemas
$ .venv/bin/python -m pytest tests     # 133 tests, fake Google Benchmark binary, no compiler
$ ./demo.sh                             # real builds; needs cmake, a C++ compiler, network once
```

`demo.sh` prints every command before running it, as you would type it
(`bx` for `.venv/bin/python -m benchx.cli`, `$WORK` for its scratch
directory), so it doubles as a walkthrough. It prepares a git source with two
commits and three CMake build directories of `examples/demo-suite` (base,
head, and head with `DEMO_HARDENED=ON`), shows the first work order of each
part, then:

1. **revisions, ad hoc:** alternates base and head over five rounds with orders
   that name no project, and compares them through a throwaway store;
2. **environments, tracked:** alternates plain and hardened, labeled
   `build: plain|hardened`, compares from the local store, re-ingests
   (no-op), and rejects a unit conflict;
3. checks that the throwaway store gives the same comparison document as the
   persistent one, lists series and history, and ends with `bx head`.

Every `bx run` delivers to the local store, so each demo run adds its
results, ad hoc and tracked, to `~/.benchx/store.parquet`. Run keys carry a
timestamp, so runs accumulate and each series' history grows. Look at it any
time:

```console
$ .venv/bin/python -m benchx.cli head -n 20
$ rm ~/.benchx/store.parquet    # start over
$ BENCHX_HOME=/tmp/benchx ./demo.sh    # or keep the demo out of your real store
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
  runner.py          one work order in, results out
  identity.py        fingerprints, identity policy, series points (schema §4.3, §4.5)
  parquet.py         the store's row: #30's columns plus derived ones
  store.py           the single-file Parquet store and the ingest contract (schema §5.4)
  compare.py         run mode, profiles, comparison document (comparator.md)
  cli.py             bx
../schemas/          the result and work-order schemas, read in place ($BENCHX_SCHEMAS overrides)
examples/demo-suite  a two-benchmark Google Benchmark suite for the demo
tests/               test_prototype.py: the success criteria and the runner and store
                     rules, on a fake Google Benchmark binary; test_comparator.py: the
                     comparator's invariants and method on hand-built results;
                     test_loader, test_validation, test_rules, test_pipeline,
                     test_validate: core and `bx validate`, on the schema's examples
```
