# PLAN: Comparator — separate evidence structure from statistical method

## Goal

Restructure the comparator so that new comparison methods and change detectors
can be added without changing the framework, the comparison-document schema, or
each other. Keep both run mode and history mode; the comparator's name is out of
scope.

## Read first

- `docs/design/comparator.md` — current design (two modes, *k* × noise)
- `docs/design/benchmark-result-schema.md` §4.3 (projections), §5.3 (`round`,
  `slot`, observation `pair` keys), §5.5 (profiles)
- `docs/design/system-decomposition.md` §3.6, §3.7, §6.1 ("a statistics library,
  not a shape")
- `src/benchx/projection.py`, `src/benchx/compare.py`, `tests/test_comparator.py`
- `src/benchx/schemas/comparison-document/0.1.0/schema.json`

## Decisions

1. **Pipeline.** `Selector → Evidence → Method → Assessment → comparison document`.
   Detectors are a sibling: `Series → Detector → Findings` (boundary + Assessment).
2. **Selectors are modular, versioned code; `Evidence` is a fixed, recorded
   schema.** Built-ins: run-profile (run mode), preceding-window (history mode),
   explicit (caller picks points; still validated to lie in one series).
3. **Evidence is a flat table, not two buckets.** One row per attempt estimate
   (observations attached), with columns read by **`Key`s**: the side axis,
   `procedure.round`, `procedure.slot`, `run_key`, `attempt_key`,
   `started_at`, axis order. Rows are grouped into units (one per series). The
   framework derives a few facts by counting: is a key crossed with the side (a
   block, e.g. `round`) or nested in it (a replicate, e.g. `attempt_key`); are
   blocks complete; did slots alternate. Treatments are not baked in: only the
   dimensions a method asks for get special meaning.

   History evidence adds the target, the ordered reference, and boundaries.
   Optional: **calibration history** (past comparisons in the series, as
   rustc-perf uses for its threshold).
4. **Methods own all statistics**: effect measure (difference, relative, log
   ratio, ratio of means), noise model, decision rule, minimum evidence, config
   schema, details schema. A method declares the dimensions it needs as a plain
   tuple, like a gufunc's core dimensions, e.g. `dims=("round", "side")`; the
   framework pivots each unit into that shape (missing cells masked) and loops
   over units. If a unit can't be laid out that way, the method doesn't apply
   and the framework reports why; within that, the method may still return
   `indeterminate` with its own reason.
5. **Assessment (shared)**: verdict, reason, optional headline effect on a
   declared measure, used/excluded result refs, method-owned `details`.
   Deterministic given inputs + method version + config.
6. **Invariants split by owner.** Projection/profile: `two-sides`,
   `one-tree-per-side`, `one-benchmark-tree`, `sides-differ`. Method
   requirements: `equal-attempts`, `alternating-slots`, `duplicate-points`.
7. **Units are series.** Grouping a run by the profile projection's series
   fingerprint reproduces `compare.py`'s hand-built units (verified on the test
   fixtures); sides that disagree fall into two one-sided series, which replaces
   the `sides-agree` check.

## Steps

1. **Docs.** Rewrite `comparator.md` §2–§5 per the decisions. Add three worked
   examples on one series (e.g. NumPy `sort`, float64, 10⁶):
   - run mode: paired by round; pairing cuts the standard error ~10× vs two
     buckets, whatever the effect measure;
   - history mode: latest point vs 12 ancestors; spread between nights (~3%)
     hides a 9% regression at *k* = 3;
   - anchored history: a fixed release build measured each night; `run_key` is
     crossed with build kind (subject vs anchor), so each night is a block;
     spread drops to ~0.5%.

   Open questions 1–2 become "which method does the project configure".
2. **Schema** `comparison-document/0.2.0`: record `selector` {name, version,
   params} and an evidence summary (sides, the keys used and whether each is
   crossed with or nested in the side, ordering, boundaries) instead
   of mode-specific fields; merge `runUnit`/`historyUnit`; outcome becomes
   {method, verdict, reason, effect?, used, excluded, details}, with details
   validated by the method's schema.
3. **Groundwork: factor `Key` out of `projection.py`.** A `Key` is a JSON
   pointer + optional normalization rule + ordering, serializable to JSON (so
   views can become data later without changing it). `Projection` uses `Key`s
   for its axis and roles; no behavior change, and `tests/test_projection.py`
   must pass unchanged. Add one pivot helper (unit rows → array over named
   dims, with a mask). Build only what steps 4–6 call.
4. **Code.** Add `Evidence`, `Assessment`, `Selector`, `Method` (new module,
   e.g. `src/benchx/evidence.py`). Port run mode: a run-profile selector that
   groups by profile series fingerprint; move `_unit`'s statistics into
   `benchx/paired-relative/v1` with `dims=("round", "side")`. Output must stay
   equivalent.
5. **Prove extensibility.** Add a second method with different needs (e.g.
   UC-02's fixed margin on the minimum, or Mann–Whitney on attempt estimates,
   `dims=("side",)`, unpaired), touching only its own module and the registry.
6. **History and detectors: design, plus test/prototype code only.** In
   `comparator.md`, specify the preceding-window selector, its evidence (target,
   ordered reference, boundaries) and `Detector.scan(series) -> list[Finding]`,
   with accepted findings becoming `change-point` boundaries. To check that the
   `Evidence` / `Method` / `Assessment` types from step 4 can express them,
   write a minimal preceding-window selector, latest-point method and trivial
   detector in tests or a clearly marked prototype module (e.g. the three worked
   examples as test cases). Nothing user-facing: no CLI, no config.

## Fix along the way

- "Noise" is a standard error in run mode (`compare.py`) but reads as a standard
  deviation in history (`comparator.md` §4 example). Make it method-defined and
  record which in `details`.
- A fixed *k* means different confidence at different *n* (*k* = 3: ~90% at 3
  rounds, ~99% at 20). Methods record degrees of freedom or use quantiles.
- Schema §5.5 offers `{role, size}` as a two-factor design, but `two-sides`
  refuses more than two sides.
- Gap: one side vs one side across runs (release vs previous release). Covered
  by calibration history or the explicit selector.
- Consider an `equivalent` verdict (equivalence tests claim "same", which is
  stronger than "no change detected").

## Done when

- The behavioral assertions in `tests/test_comparator.py` (verdicts, effects,
  invariants) still hold after the port; document-shape assertions are updated
  to the new schema.
- `tests/test_projection.py` passes unchanged after `Key` is factored out.
- The second method lands without edits outside its module and the registry.
- `comparator.md` describes the framework/method boundary with the three examples.

## Non-goals

Renaming the comparator; drafting use cases (users write those); a user-facing
history mode or detector (test/prototype code only, per step 5); a real
change-point algorithm; choosing default methods beyond porting the current one;
a revision graph or ancestry.

**Deferred groundwork** (add when a second caller exists): projections/views as
data with `where` and `base` (e.g. workload-specific projections; until then,
build them in code with `Projection(policy, axis, paths=...)`); join and lag
operators; moving `Store.head` / `series` / `history` onto `Key`s; a signature
parser or broadcasting beyond looping over units; DuckDB or any query language.
