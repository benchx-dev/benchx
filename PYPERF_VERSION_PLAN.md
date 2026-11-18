# Plan: a function-source hash for the pyperf adapter

Branch `pyperf-function-version`, from `main`. Go ahead given with decisions 1, 3 and 5
as proposed (the key name, the optional `version` attribute, comments count in the
hash). The mapping version stays `v1` at the user's request (decision 7).

Problem. asv guards against a changed benchmark: each benchmark has a `version` (a hash
of its source), a result of another version is not comparable, and the benchx asv
adapter records it (`protocol.asv.benchmark_version`), so `sides-agree` fails the unit.
The pyperf adapter records nothing of the kind. A benchmark function that is **in the
subject's tree** and is edited between two revisions is compared silently: its
`benchmark_tree` is the subject's tree, which "differs between the sides exactly as the
subject trees do and is not checked" (benchmark-result-schema.md §4.1). A benchmark
**outside** the tree is guarded only at file grain (`one-benchmark-tree`: the file must
be the same on both sides), and an edit made before a run starts is not linked to older
results at all.

Also in this plan, found while checking asv's side of it: `bx bench` with an asv suite
outside the subject's checkout crashes with a traceback. It gets a clean refusal
(decisions 10 and 11, the discussion, steps 6 to 8).

## Check against the design docs

1. **Docs say** (benchmark-result-schema.md §5.5, profile table): Under `revisions`,
   `variants` and `environments`, **protocol** must agree between the sides. **For the
   plan:** A hash in `coordinates.protocol` is checked by `sides-agree` with **no
   comparator change**.
2. **Docs say** (identity.py; prototype-design.md "identity policy"): The identity
   policy `benchx/benchx-identity/v1` ignores the benchmark *revision* and host facts,
   not protocol. **For the plan:** A changed hash starts a new series, as for asv. The
   reason for ignoring the revision (it changes on every commit and would split history
   at every point) does not apply: the hash changes only when the function does.
3. **Docs say** (measurement-result schema): The `benchmark` block is closed: `source`,
   `revision`, nothing else. **For the plan:** Function-level data cannot go there.
   `protocol` is the one open place that is matched (asv's precedent). `provenance.info`
   is open but not matched.
4. **Docs say** (prototype-design.md, identity policy): "The implementation has no
   declared benchmark versions". **For the plan:** Still true: a *declared* version is a
   project's own mapping (schema §4.1). A computed hash is a different thing. The
   sentence is reworded, not contradicted.
5. **Docs say** (harness-adapter.md principle 7, "Mapping changelog"): "The mapping is
   versioned: changing a rule changes `mapping_version`"; each version documents what
   changed. **For the plan:** The version stays `pyperf-to-benchx/v1`. That is the
   user's decision (decision 7), which overrides this principle for now. The asv adapter
   has its own mapping (`asv-on-pyperf/v2`) and its own `case_protocol`, so it does not
   change either. Old native files cannot be re-translated into versions (they hold no
   source).
6. **Docs say** (harness-adapter.md §6.8; pyperf.py docstring): The adapter borrows
   asv's attribute conventions: `params`, `param_names`; `setup`, `teardown`,
   `skip_params` "are not read". **For the plan:** An optional `version` attribute fits
   the same pattern.
7. **Docs say** (comparator.md §3; prototype-design.md): `sides-agree` affects one unit;
   the others are compared. **For the plan:** On `main` this is not yet true: a failed
   invariant leaves no units for the whole run. The per-unit behaviour is a separate
   change, on `asv-style-tables`. So the tests here assert the failed invariant only,
   and hold on either side of that change.
8. **Docs say** (compare.py `_diff`): The comparator compares facts in `protocol`; a
   fact absent on one side and present on the other differs. **For the plan:** If the
   source cannot be read on one side only, that unit fails `sides-agree` (we cannot
   vouch it is unchanged). Both sides without it agree.
9. **Docs say** (schema): The schema describes `protocol` as "how the subject is
   exercised and measured". **For the plan:** A code hash is not strictly that. It is a
   judgement call, made already by asv's `benchmark_version`. Flagged, not blocking.
10. **Docs say** (benchx.md "asv" paragraph): asv's hash is in
   `protocol.asv.benchmark_version` and "editing a benchmark starts a new series". **For
   the plan:** The pyperf sentence goes next to it.
11. **Docs say** (grep of docs/): The asv adapter's hash is **not in the design docs at
   all**: only benchx.md, prototype.md and the adapter's docstring (`docs/design/` has
   no `benchmark_version`). The schema §4.1 sentence "in-tree benchmarks are not
   checked" is therefore already inexact for asv, which does check them. **For the
   plan:** The design passages are written once, for **any adapter that records a hash
   of each benchmark's code in `protocol`** (asv and pyperf). That closes the asv gap in
   the same edit. It is documentation only: asv's code does not change.

Nothing in the docs contradicts the plan. Four passages need a sentence (step 3), each
for both adapters: prototype-design.md's "no declared benchmark versions" (row 4),
harness-adapter.md §6.8 (rows 5, 6), the schema §4.1 passage that says in-tree
benchmarks "are not checked" (it gains an exception), and benchx.md's asv sentence (row
10). comparator.md needs nothing (row 7).

## Decisions

1. **What is hashed:** SHA-256 of the benchmark function's source as asv_runner makes
   it: `inspect.getsourcelines`, trailing whitespace stripped from each line, dedented.
   Comments and docstrings count (asv's primary hash). **Not** hashed: the values of
   `params`, helper functions the function calls, imports, the library under test. The
   adapter reads no `setup`, so there is none to hash.
2. **Computed in the discovery step** (`LISTER`, which runs in the benchmark's own
   Python and has the function objects). `run_case` carries it to the result.
3. **Recorded as** `coordinates.protocol.pyperf.benchmark_version` (a hex string),
   written by a new `case_protocol` hook of the pyperf adapter. The name mirrors
   `protocol.asv.benchmark_version`. No schema change, no schema version bump
   (`protocol` is an open object).
4. **Source unavailable** (a C function, a lambda, code made by `exec`): the key is
   omitted for that case. See row 8 above for what the comparison then does.
5. **Optional `version` attribute** on the function overrides the hash (asv's
   convention). It lets a project keep a series across cosmetic edits. Recommended;
   strike it if unwanted.
6. **No comparator change.** The failure reads
   `sides-agree: /coordinates/protocol/pyperf/benchmark_version differ` and names the
   unit. What else the comparison reports then depends on the per-unit change (row 7 of
   the check).
7. **The mapping version stays `pyperf-to-benchx/v1`.** The user decided this,
   overriding the design's rule that a changed mapping changes its version
   (harness-adapter.md principle 7). The consequence is accepted: results from before
   and after this change both say `v1`, and only the new ones carry
   `protocol.pyperf.benchmark_version`. The asv adapter is untouched. To revisit with
   the next mapping change.
8. **Known consequence:** stored pyperf results made before have no key, so their series
   and the new ones are different series. History splits once, at the upgrade.
9. **Known limit** (same as asv): an edit to a helper the function calls is not seen.
   For a suite outside the tree, the file hash (`one-benchmark-tree`) still covers it.

10. **Refuse an asv suite outside the subject's checkout, cleanly.** In
   `session._found_benchmark`, where the order's benchmark identity is computed: when
   the suite is outside the subject's checkout and has no identity (a directory has no
   blob id), raise a `SessionError` that says so and says to put the suite inside the
   checkout. The CLI prints it as `bx bench: error: ...` and refuses before anything
   runs, as for any refused order.
11. **Not in the adapter's `locate`.** With the provider, the target path is the build
   directory, and NumPy's asv suite, given by its absolute path, is inside the *source*
   checkout and must keep working. Only the session knows both paths.

12. **Known consequence: a benchmark function that is itself the subject.** The hash is
    matched under `revisions`, so two revisions whose function text differs fail
    `sides-agree`, as for asv. That is the point for a function that exercises library
    code elsewhere in the tree. It blocks a comparison when the function body is what
    changed (a micro-benchmark script, or the fixtures of `test_compare_harnesses.py`,
    which edit `sum(range(200))` to `2000`). The way out is the optional `version`
    attribute (decision 5), or keeping the changing value outside the function. The
    fixtures move the value to the module (a limit that decision 9 already names). Any
    other branch or example that compares such functions needs the same change.

13. **Merging with the open branches** (`asv-style-tables`, `filter-refusal-hints`). New
    tests go in new files (`tests/test_benchmark_version.py`), not appended to
    `test_params.py`, `test_pyperf.py` or `test_asv.py`, which both branches extend. The
    docs hunks avoid the invariants paragraph of comparator.md and prototype-design.md
    that `asv-style-tables` edits. The per-unit `sides-agree` behaviour exists only
    there, so no test here depends on it.

## Discussion: asv suites outside the checkout

**What happens today.** `bx bench TARGET --adapter asv --suite DIR`, with DIR outside
the checkout, fails with a traceback that ends:

    RuntimeError: the error result is itself invalid: malformed at
    /provenance/benchmark_tree: None is not of type 'string'

The shorthand `bx bench DIR` already refuses cleanly ("not inside the tree"), so only
the long form is affected.

**Why.** A suite outside the subject is identified by its file's blob id, so that
`one-benchmark-tree` can require one file on both sides. The design says so: "the file
is the unit, not its directory" (runner-schema.md §3.3). An asv suite is a directory of
modules, `blob_id` cannot read it and returns `None`, and the result fails its own
validation. A pyperf suite is one file, so it works.

**Why refuse, not support.** Supporting it is a design decision, not a bug fix: what
identifies a directory (a hash over which files: the `.py` files only, `asv.conf.json`,
the `__pycache__` that appears when it is imported?), and a change to the text of
runner-schema.md §3.3 and the schema's §4.1 and §5.5. There is no use case for it now:
NumPy's suite, the one in use, is inside the checkout, and works through the provider
target. The refusal costs nothing that works today, and it replaces a traceback with a
message. The numpy-revisions README line ("does not work with `bx bench` yet") stays
true.

**If it is wanted later.** (a) A tree hash over the directory's `.py` files and
`asv.conf.json`: the least change, but it departs from "the file is the unit". (b) One
id per module file, so several `benchmark_tree` values in a result: a schema change. (a)
is the likelier choice.

**Relation to the function hash.** Independent. The hash guards a function inside the
tree; for a suite outside it the file-level check applies, where it can be computed.
Out-of-tree pyperf suites keep both.

## Steps

- [x] 1. Tests first (they fail now): the hash (stable under trailing whitespace; a
      comment, a code change or a rename of a local changes it; `params` do not); the
      listing carries it; a result has `protocol.pyperf.benchmark_version`; an explicit
      `version` wins; source unavailable omits it; two trees whose function differs fail
      `sides-agree` (the invariant only: see row 7 of the check) and two that agree do
      not; asv results have no `protocol.pyperf`; `mapping_version` is unchanged
- [x] 2. `adapters/pyperf.py`: `LISTER` (version), `list_cases` info, `run_case` result,
      `case_protocol`, module docstring
- [x] 3. Docs, each written for both adapters, because asv's hash is undocumented in the
      design docs today: harness-adapter.md (§6.8), benchmark-result-schema.md §4.1 (the
      in-tree exception), prototype-design.md (identity policy sentence), benchx.md (the
      pyperf paragraph beside asv's, and the asv refusal). comparator.md is not edited:
      protocol was already matched, and its invariants paragraph is the one
      `asv-style-tables` changes.
- [x] 4. Full suite, both environments
- [x] 5. Real run on a stand-in tree with two commits where one function is edited: the
      comparison names that unit under failed invariants. On `main` it then has no
      verdict for the other function either (the per-unit behaviour is on
      `asv-style-tables`). No NumPy run: the scratch clone is gone.
- [x] 6. Tests first (they fail now): `bx bench TARGET --adapter asv --suite DIR` with
      DIR outside the checkout is refused with the message, with the exit status of a
      refused order, no traceback and no results written; an asv suite inside the
      checkout still runs; a build target whose source checkout holds the suite
      (absolute path) still runs; a pyperf file outside the tree still runs
- [x] 7. `session._found_benchmark`: raise the `SessionError`
- [x] 8. Docs: one sentence in benchx.md's asv paragraph; the numpy-revisions README
      note (refused with a message). Its own commit, apart from the hash

## Outcome

New file `tests/test_benchmark_version.py` (18 tests, all written to fail first and
failing for the missing feature). Full suite, both environments: 658 passed, 7 skipped.
Three existing tests broke, as decision 12 predicted: the fixtures of
`test_compare_harnesses.py` edited the function body between commits. They now change a
module constant instead. Nothing else in the suite compared functions that differ.

Changed: `adapters/benchx_params.py` (`source_version`), `adapters/pyperf.py` (lister,
`run_case`, `case_protocol`, docstring), `session.py` (the refusal), the two fixtures,
four docs, and one line of the numpy-revisions README in benchx_user. The mapping
version is `v1`, as decided.
