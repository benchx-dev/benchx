# Comparator

**Status:** Draft for review
**Companion to:** `system-decomposition.md` §3.6, `benchmark-result-schema.md`

Terms follow the schema's vocabulary (§2): a *series* is one comparison identity
× one estimator under one identity-policy schema, and a *series point* is the
estimate one result contributes to it (§4.1).

## 1. Role

The comparator answers "did performance change?" in two modes that share one
framework and one output document:

- **History mode** evaluates an explicit target series point against a
  reference formed from preceding points in the same series. The target is
  normally the newest point selected by the caller, but it is an immutable input
  to the comparison; the comparator never silently substitutes an earlier point.
- **Run mode** evaluates two sides measured in one run, as a comparison profile
  (schema §5.5) defines them: two revisions of the subject (UC-03), two workload
  variants (UC-02), or two environments (UC-04). The baseline side plays the role
  of the reference.

A mode is a selector, not a code path: it decides which results form the
evidence, and a method decides what the evidence shows. A method classifies the
target as improved, regressed, no change detected, or indeterminate, and may
claim equivalence. Every verdict carries its evidence, and insufficient evidence
is reported rather than ignored.

The comparator is a side-effect-free evaluator and runs on demand. The change
detector decides when and which series to evaluate continuously, while policy
decides whether a verdict should block or alert.

**Implemented today:** run mode (`run-profile`), and the framework it runs on.
History mode and detectors are specified here and exercised by test-only
prototypes (`tests/test_history_prototypes.py`); they have no CLI and no
configuration.

## 2. Input

Evaluation is a pipeline with one fixed schema in the middle:

```text
Selector -> Evidence -> Method -> Assessment -> comparison document
Series   -> Detector -> Findings (a boundary and an Assessment)
```

Selectors and methods are modular, versioned code. `Evidence` and `Assessment`
are fixed, so a new method needs no new selector, a new selector needs no new
method, and neither changes the comparison document.

### 2.1 Store boundary

All inputs come from the result store; result documents are ingested before
comparison. An offline workbench uses a local store, which may be ephemeral, so
local and hosted use follow the same path.

The caller names what to compare, not the points themselves: a comparison
identity or series fingerprint and a target in history mode, a `run_key`, a
profile, and its varying coordinate in run mode. The comparator retrieves the
corresponding series points from the store. Series membership is determined by
the store's identity policy and is intentionally configurable; the comparator
never assigns it, and every comparator and change detector sees the same
membership for the same policy.

This is also how history survives a change the identity policy knows about,
such as a benchmark rename or a decommissioned machine replaced by a new one. A
continuity mapping in the identity policy (schema §4.3) joins the old and new
results into one series, and the join appears as a boundary in the evidence
(§2.2). Without such a mapping, the new machine starts a new series and the
comparator has no reference to evaluate against.

### 2.2 History mode: the `preceding-window` selector

The target and reference points belong to one series under one identity-policy
schema and use one estimator. The target is explicit so a comparison remains
stable when newer results arrive. The selector's parameters are the target and
the window; its evidence has two sides, `reference` and `target`, plus the
ordered reference and the boundaries that shaped it (§2.6).

A source with a revision graph uses the target's ancestors, not an arbitrary
linearization of other branches. When ancestry is unavailable, the store may
supply a native linear order. Without either, the reference cannot be formed and
the verdict is indeterminate.

The window stops at the most recent distribution reset or accepted change point
before the target, so measurements from an obsolete performance regime do not
inflate current noise, and it crosses a continuity join. Every boundary that
truncates or joins history is included in the evidence.

**Anchored history** uses the same selector with sides named by a label, such as
`subject` and `anchor`, where the anchor is a fixed release build measured every
night. Under the default identity policy the anchor and the subject are one
series, because the revision is the axis and nothing else differs: the selector
must tell them apart by the label, or anchor measurements enter the subject's
reference as if they were history (§8.3).

### 2.3 Run mode: the `run-profile` selector

A run-mode request names a `run_key`, a profile, and the coordinate the profile
varies, for example the subject tree id for `revisions` or `parameters.role` for
`variants`, and which value is the baseline. Sides are chosen at comparison time
(schema §5.5): each distinct value of the varying coordinate is one side.

**Units are series.** Results are grouped by their series fingerprint under the
profile's projection, whose axis is the varying coordinate, so everything else
the projection matches must agree for two results to share a unit. Each unit is
evaluated independently. A result that belongs to a series under the identity
policy still records that series (`side_series`), but run mode reads no history
outside the run.

Sides that disagree on a matched fact therefore fall into separate series. That
is the same shape as a workload measured on one side only, which is merely
`missing`, so the selector tells them apart: several series with one workload
variant and quantity are a failed `sides-agree`, naming the facts that differ. A
disagreement fails the whole run; a variant on one side only does not.

### 2.4 Batch requests

A batch request identifies a target run or revision and the series expected for
that target. It evaluates each target independently and reports an expected
series with no target result as missing; it never substitutes that series' most
recent result from an older run.

### 2.5 The `explicit` selector

The caller lists the points. They are still validated to lie in one series, so
this selector cannot compare what a projection keeps apart. It covers one side
against one side across runs, such as a release against the previous release.
One point per side has no spread to estimate noise from, so a method needs
calibration history (§2.6) or a declared provisional rule.

### 2.6 Evidence

Evidence is a flat table, not two buckets. Each row is one attempt's estimate
with its observations attached, and each column is read by a `Key`: a JSON
Pointer, an optional normalization rule, and an ordering, serializable to JSON.

| Column | Key | Typical relation to the side |
|---|---|---|
| `side` | the profile's axis, or the selector's own labels | |
| `round` | `/procedure/round` | crossed: a block |
| `slot` | `/procedure/slot` | nested: unique to one attempt |
| `run_key` | `/provenance/run_key` | crossed in anchored history; constant in a run |
| `attempt_key` | `/attempt_key` | nested: a replicate |
| `started_at` | `/provenance/started_at` | nested |
| `revision` | `/revision/key` | crossed in anchored history if a build is shared; otherwise nested |

Rows are grouped into units, one per series. The framework derives a few facts
by counting, and gives no treatment special meaning beyond them:

- **Crossed or nested.** A key is *crossed* with the side when some value of it
  appears on more than one side, which makes it a block. It is *nested* when
  every value appears on one side only, which makes it a replicate. It is
  *absent* when no row has it. Failed attempts count: the design of the run is
  what is being read, not which attempts happened to succeed.
- **Complete.** A crossed key is complete when every value appears on every side.
- **Alternation.** Whether the sides alternate by `procedure.slot`. This is judged
  over the whole run, not within a unit: a slot is a position in the run's
  realized order, and a unit that skipped one attempt has gaps that say nothing
  about the schedule.

History evidence adds the target, the reference in order oldest first, the
ordering used, and the boundaries. It may add **calibration history**: past
comparisons in the series, from which a method can set its threshold, as
rustc-perf does.

## 3. Eligibility guard

Checks belong to whoever needs them to be true. A failure is recorded in
`failed_invariants` with its `owner`, and the affected units get no verdict.

**The framework** checks that a result has a usable estimate. Anything else is
excluded with its reason, and a target without one is indeterminate, never
replaced by an older successful point. Excluded points and their reasons remain
visible in the output.

**The selector or profile** checks what makes the sides meaningful, and a failure
stops the whole run:

| Invariant | Profile | Meaning |
|---|---|---|
| `sides` | all | every result can be named by the varying coordinate |
| `two-sides` | all | exactly two sides |
| `sides-agree` | all | one workload variant and quantity is one series (§2.3) |
| `one-tree-per-side` | `revisions` | each side has one tree id, and the two differ |
| `one-benchmark-tree` | `revisions`, `variants` | a separately recorded benchmark is one file on both sides and in every round (schema §5.5) |
| `sides-differ` | `environments` | the sides differ in more than their label |

A side that is not clean does not fail the run; it makes the comparison
local-only (UC-03 §10). In history mode the selector checks that the target and
every reference point have the same series fingerprint and that every reference
point precedes the target under the selected ancestry or native order.

The selector also owns the **status rule**, which is not an invariant: it leaves
one unit without a verdict and fails nothing else. Under `variants` it is strict
(UC-02 §10): a unit with an attempt of status `partial`, `censored` or `error`, on
either side, is indeterminate with reason `side-failed`, whatever the other rounds
show; otherwise a variant that times out in the slow rounds is judged on the fast
ones. The selector records this in the evidence (`no_verdict`), every method must
return `indeterminate` with that reason, and the framework refuses a method that
does not, so the rule holds whichever method a project configures. The other
profiles leave the remaining attempts to the method, and a `skipped` attempt is
only excluded under every profile. The schema's §5.5 table is not touched.

**The method** declares what its statistics assume, and the framework checks
what it declared:

| Requirement | Scope | Meaning |
|---|---|---|
| `alternating-slots` | run | the schedule alternated between the sides |
| `equal-attempts` | unit | as many attempts on each side |
| `duplicate-points` | unit | no cell of the method's layout holds two usable results; the comparator never picks one implicitly |

A method that does not require alternation may still evaluate a run that did not
alternate. The document records the schedule, and the unit carries the caveat
`sides-did-not-alternate`.

`duplicate-points` as a requirement sees one cell of the layout. A revision that
contributes two points to a history reference is a different case, since a
`("side",)` cell holds many results by design and `revision` is nested in the side,
so it cannot be a dimension. A history method reads the `revision` column and
returns `indeterminate` with reason `duplicate-points` unless it declares how it
combines them; the comparator never picks one implicitly.

**The layout.** A method declares the dimensions it needs (§4.1). When a unit
cannot be laid out that way, because a dimension is absent or nested in the side,
the method does not apply: the failure is recorded as `layout`, with the reason.
Within a layout the method may still return `indeterminate` with its own reason.

Changes in facts the projection ignores do not split a series by themselves, but
differences between the target and its reference are carried as caveats so that a
kernel, load, or other recorded condition is not hidden from the reader.

An intentional comparison across an identity difference outside a single run
requires the series to be materialized under an explicit projection that
projects that difference out (schema §4.3). The comparator never bypasses the resulting
series boundary.

## 4. Methods and verdicts

### 4.1 What a method owns

A method has a stable name and version, and owns all the statistics: the effect
measure (difference, relative, log ratio, ratio of means), the noise model, the
decision rule, its minimum evidence, and the schemas of its configuration and
its details.

A method declares its dimensions as a plain tuple, like a generalized ufunc's
core dimensions, and the requirements of §3:

```python
class Method:
    name = "benchx/paired-relative"
    version = "v1"
    dims = ("round", "side")
    requires = ("equal-attempts", "alternating-slots", "duplicate-points")

    def assess(self, layout, evidence, config) -> Assessment: ...
```

The framework pivots each unit into that shape and loops over units. A cell holds
the usable rows at those coordinates, and a cell with none is masked. So
`("round", "side")` is a matrix of paired attempts, and `("side",)` is the
attempts of each side, of unequal length if they differ.

The estimator is not the method's: it belongs to the series (§4.3 of the schema),
which is what the selector grouped by.

### 4.2 The Assessment

Every method returns the same record:

- **verdict**: `improved`, `regressed`, `changed` (a change with no known
  direction), `no-change-detected`, `equivalent`, or `indeterminate`;
- **reason**, required when indeterminate, such as `insufficient-rounds`;
- **effect**, optional: the headline change, on a measure the method declares;
- **used** and **excluded**: every result of the unit is in exactly one of them,
  and each exclusion has a reason;
- **details**: anything else, validated by the method's own schema.

An Assessment is deterministic given its inputs, the method version, and the
configuration.

`no-change-detected` and `equivalent` are different claims. The first says a test
did not find a change, which low power also produces. The second is what an
equivalence test claims: any change is smaller than a declared margin. No current
method returns `equivalent`; the verdict is in the schema so that one can be added
without changing it.

### 4.3 Noise is defined by the method

"Noise" has no meaning of its own, so each method records what it computed in
`details`. The choice can decide the verdict. For a single new point against a
reference, the relevant spread is that of the points, because the target is one
more draw. The standard error of the reference shrinks with the window and answers
a different question. On the data of §8.2 the first gives a threshold of 10.1%
and the second 2.9%, so they disagree about a 9% change.

A fixed multiplier also means different confidence at different sample sizes. With
*k* = 3 on the standard error of *n* paired differences, the two-sided confidence
under a t distribution is about 80% at 2 rounds, 90% at 3, 96% at 5, and 99% at 20.
A method therefore records its degrees of freedom or uses quantiles.

### 4.4 `benchx/paired-relative/v1`

Requires alternating slots, equal attempts, and no duplicate points. Per round it
computes d = contender / baseline − 1 from each side's estimate.

- **Effect:** the mean of d, measure `relative`.
- **Noise:** the standard error of d across rounds, recorded with `noise_kind:
  standard-error` and its `degrees_of_freedom`.
- **Decision:** a change when |effect| > *k* × noise, labeled by the quantity's
  direction when known, otherwise `changed`.
- **Indeterminate:** `insufficient-rounds` below `min_rounds` pairs, and never with
  fewer than two since the noise needs a spread; `zero-baseline` when a baseline
  estimate is zero; `deterministic-deferred` for a quantity declared deterministic.

Exact equality would be the natural rule for a deterministic quantity (schema
§4.4), which needs no noise estimate. No quantity of the implementation is
deterministic, so that is deferred.

### 4.5 `benchx/mann-whitney/v1`

Requires nothing of the run's schedule or pairing: `dims = ("side",)`. It tests
whether one side's attempt estimates tend to exceed the other's, and is the
unpaired method for a run that did not alternate.

- **Effect:** the relative difference of the sides' medians, measure
  `relative-median`, absent when the baseline median is zero.
- **Decision:** a change when the two-sided p-value is at most `alpha`, labeled by
  the direction of the rank shift. The p-value is exact, with ties handled by
  permuting midranks, up to 40 attempts in total; beyond that it is a tie-corrected
  normal approximation.
- **Indeterminate:** `insufficient-attempts` when the design cannot reach `alpha`
  at all. The smallest two-sided p-value is 2 / C(n + m, n), which is 0.1 for 3
  attempts per side and 0.029 for 4. A run too small to detect anything does not
  get to report that it detected nothing.

It ignores the blocking in a paired run, so on the same run it is usually the less
sensitive of the two (§8.1). That is the cost of asking less of the run.

### 4.6 Change detectors

A detector is a sibling of a method, sharing the Assessment and not the pipeline:

```text
Detector.scan(series) -> list[Finding]
Finding = {boundary, assessment}
```

A series is the ordered points of §2.2. A finding locates a change: its `boundary`
is a `change-point` at the first result after it, and its assessment says what was
found there, in the same vocabulary as a method's. An accepted finding becomes a
`change-point` boundary, which ends the reference of later evaluations (§2.2), so
a regime change is reported once and then stops inflating noise. Acceptance is
review or policy, not part of the detector.

## 5. Output

A **comparison document** (`schemas/comparison-document/0.2.0`) is machine-readable
and self-describing. It records:

- the `selector` as `{name, version, params}`, and the `method` requested with its
  complete configuration, present even when a failed invariant left no units;
- `sides`, for a selector that has named ones: the varying coordinate, the two
  values, and what each side declared about how it was built;
- the identity-policy schema, the estimator, and `local_only`;
- `units`, and for each: the workload, quantity, and series fingerprint; an
  `evidence` summary; the outcome; and the caveats;
- exclusions, failed invariants with their owners, missing targets, and every input.

A unit's **evidence summary** replaces mode-specific fields. It holds the keys the
method used, each with its relation to the side (`crossed`, `nested`, or `absent`)
and whether it is complete; the slot schedule; and in history mode the target,
reference, ordering, and boundaries.

A unit's **outcome** is the Assessment: `method`, `verdict`, `reason`, `effect` as
`{measure, value}`, `used`, `excluded`, and `details`. The framework validates
`details` against the method's schema when it produces the document. The document
schema itself treats `details` as an open object, since a new method must not
change it, so `bx validate` does not check them.

The document is renderable as a reviewer-speed summary ("these target results
regressed, these improved, these show no detected change, these were
indeterminate, and these were missing") and consumable by alerting or CI gates.

Comparison documents are derived and recomputed on demand; the result store
persists their immutable inputs rather than the documents themselves. Supplying
the recorded inputs, selector, method version, and configuration reproduces the
document even after newer points arrive.

## 6. Boundaries

- **Not target selection:** selecting "latest", a target run, or a target
  revision happens before invoking the comparator.
- **Not an arbitrary cross-series pair engine:** every selector stays within one
  series, or within the sides a profile defines in one run.
- **Not policy:** which method a project uses, its configuration, and gating
  decisions are project configuration; the comparator reports, humans and
  policy decide.
- **Not the detector:** it consumes a fixed historical reference but does not
  watch series, schedule evaluation, detect drift, or locate change points.
- **Not the identity authority:** series membership comes from the store's
  identity policy; the comparator only validates and consumes it.
- **Not a data source:** every number in the output is traceable to input results.

## 7. Open questions

1. **Which method does a project configure?** Earlier drafts asked which single
   method run mode and history mode should start from, and whether an unplanned
   run that did not alternate should be refused. Both are now this question.
   Methods can coexist, so the choice is per project: `paired-relative` for a
   planned run and `mann-whitney` for an unplanned one are both available in the
   library, and `bx compare` uses `paired-relative`. UC-03's sketch reports a ratio
   with a 95% confidence interval over paired rounds, which would be a third method.
   UC-02 asks for each side's minimum against a fixed margin (§7 of the use case),
   which would be another; `variants` uses `paired-relative` until one is written.
2. **Multiple comparisons across units.** The framework calls a method once per
   unit, so no method can see the other units' results, and multiple-comparison
   control cannot be a method. It would be a step over the Assessments of a whole
   document. The need is real at the defaults: *k* = 3 at 3 rounds is about 90%
   confidence per unit (§4.3), so a suite of 100 unchanged workloads would be
   expected to report about 10 changes.

## 8. Worked examples

One series throughout: NumPy `sort`, float64, 10⁶ elements, wall time. **The values
are constructed to show the mechanism, not measured.** The tests in
`tests/test_history_prototypes.py` compute every number below from these values.
All three use *k* = 3.

### 8.1 Run mode: paired by round

Ten alternating rounds of a contender about 2% slower. Machine state moves both
sides of a round together, so the baseline spreads by 5.4% across rounds while the
ratio within a round spreads by only 0.7%.

| Treatment | Effect | Standard error | Threshold | Finding |
|---|---|---|---|---|
| Paired by round, `dims=("round", "side")` | +1.99% | 0.22% | 0.65% | regressed |
| Two buckets, `dims=("side",)` | +1.97% | 2.33% | 7.00% | no change detected |

Pairing cuts the standard error about 11× here, whichever effect measure is used:
10.8× for relative change and ratio of means, 10.9× for log ratio, 11.8× for
difference. The size of the gain comes from the data, not from pairing as such.
With between-round spread σ_b shared by both sides and within-pair spread σ_w, it
is about √(2σ_b² + σ_w²) / σ_w: 10× here, 1.7× when the two spreads are equal, and
1× when rounds share nothing.

### 8.2 History mode: the latest point against 12 ancestors

Twelve nights spread by 3.4% around a median of 41.0 ms, and the latest point is
9% above it. The spread between nights sets the threshold at 10.1%, so the method
reports `no-change-detected`. A real regression is hidden because every point
carries a night's worth of machine state, not because the method is wrong.

Using the standard error of the reference instead would detect it, with a threshold
of 2.9%, but 6 of these 12 ordinary nights are that far from the median too: a single
new point varies by the spread of the points, however many were averaged (§4.3).

### 8.3 Anchored history: a fixed build measured each night

Measure a fixed release build beside the subject each night. `run_key` is now
crossed with the build kind, so each night is a block, and the evidence fits
`dims=("run_key", "side")`, the same shape as §8.1. The night's machine state
cancels in the ratio of subject to anchor.

| | Spread | Threshold | Effect | Finding |
|---|---|---|---|---|
| Subject alone (§8.2) | 3.4% | 10.1% | +9.0% | no change detected |
| Subject / anchor | 0.46% | 1.4% | +9.0% | regressed |

Nothing about the framework is specific to anchoring. The selector names the sides,
and counting finds that `run_key` is a block.
