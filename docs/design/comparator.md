# Comparator

**Status:** Draft for review
**Companion to:** `system-decomposition.md` §3.6, `benchmark-result-schema.md`

Terms follow the schema's vocabulary (§2): a *series* is one comparison identity
× one estimator under one identity-policy schema, and a *series point* is the
estimate one result contributes to it (§4.1).

## 1. Role

The comparator answers "did performance change?" in two modes that share one
comparison method and one output document:

- **History mode** evaluates an explicit target series point against a
  reference formed from preceding points in the same series. The target is
  normally the newest point selected by the caller, but it is an immutable input
  to the comparison; the comparator never silently substitutes an earlier point.
- **Run mode** evaluates two sides measured in one run, as a comparison profile
  (schema §5.5) defines them: two revisions of the subject (UC-03), two workload
  variants (UC-02), or two environments (UC-04). The baseline side plays the role
  of the reference.

In both modes the comparator computes the effect and applies a configured
comparison method to classify the target as improved, regressed, no change
detected, or indeterminate. Every verdict carries its evidence, and insufficient
evidence is reported rather than ignored.

The comparator is a side-effect-free evaluator and runs on demand. A selector
chooses the target and reference inputs. The change detector decides when and
which series to evaluate continuously, while policy decides whether a verdict
should block or alert.

## 2. Input

The logical core contract is:

```text
evaluate(target, reference, comparison method) -> comparison document
```

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

### 2.2 History mode

The target and reference points belong to one series under one identity-policy
schema and use one estimator. The target is explicit so a comparison remains
stable when newer results arrive. Reference points precede the target and are
selected by a versioned method, for example from a bounded ancestral window.

A source with a revision graph uses the target's ancestors, not an arbitrary
linearization of other branches. When ancestry is unavailable, the store may
supply a native linear order. Without either, the reference cannot be formed and
the verdict is indeterminate.

Reference selection honors recorded distribution resets, accepted change
points, and applicable continuity events, so measurements from an obsolete
performance regime do not inflate current noise. Every boundary that truncates
or joins history is included in the comparison evidence.

### 2.3 Run mode

A run-mode request names a `run_key`, a profile, and the coordinate the profile
varies, for example the subject tree id for `revisions` or `parameters.role` for
`variants`, and which value is the baseline. Sides are chosen at comparison time
(schema §5.5): each distinct value of the varying coordinate is one side, and
every other coordinate must agree. Results that agree on everything else form
one comparison unit, typically one workload variant, and each unit is evaluated
independently.

Within a unit, the two sides' attempts are paired by `procedure.round`, so the
evidence is a set of paired differences measured close together in time rather
than two independent histories. A result that belongs to a series still carries
its series point, but run mode reads no history outside the run.

### 2.4 Batch requests

A batch request identifies a target run or revision and the series expected for
that target. It evaluates each target independently and reports an expected
series with no target result as missing; it never substitutes that series' most
recent result from an older run.

## 3. Eligibility guard

Before computing a verdict, the comparator checks, in both modes, that:

- the target has a usable estimate — otherwise that target is indeterminate, not
  replaced by an older successful point;
- every reference point or baseline result satisfies the configured status and
  quality rules;
- target and reference use the same estimator and identity-policy schema; and
- the evidence is enough for the configured method, or the method declares a
  provisional rule that applies.

In history mode it additionally checks that:

- the target and every reference point have the same series fingerprint;
- every reference point precedes the target under the selected ancestry or
  native order; and
- no revision contributes more than one eligible point unless configuration
  selects one, combines them by a declared method, or evaluates them separately.
  Otherwise the affected comparison is indeterminate with reason
  `duplicate-points`; the comparator never picks one implicitly.

In run mode it additionally checks the profile's invariants over the run (schema
§5.5): the two sides differ only in the varying coordinate, attempt counts per
side are equal, and sides alternate by `procedure.slot`; under `revisions`,
that every result of both sides has a `clean` subject and benchmark tree
(`clean-tree`, UC-03 §10). A run that fails an invariant is not compared: the comparison document records the failed invariant
and contains no verdicts for the units it affects.

Excluded points and their reasons remain visible in the output. Changes in
observed context do not split a series by themselves, but differences between
the target and its reference are carried as caveats so that a kernel, load, or
other recorded condition is not hidden from the reader.

An intentional comparison across an identity difference outside a single run
requires the store to materialize the series under an explicit identity policy
that projects that difference out. The comparator never bypasses the resulting
series boundary.

## 4. Comparison method and verdict

A comparison method has a stable name and version. It defines the reference
window or pairing, the estimator, effect measure, treatment of within-result
precision and between-point variation, minimum evidence, and decision threshold.

For the target, the method produces:

- **Reference estimate**: the expected value derived from the preceding eligible
  points in history mode, or from the baseline side in run mode.
- **Effect**: a declared measure of change from the reference estimate. Relative
  change is the default only when the reference is nonzero; the method must
  define an absolute, ratio, logarithmic, or other measure and its behavior for
  zero or negative values.
- **Noise**: variation expressed on the same scale as the effect. In history
  mode it is estimated from the reference points; in run mode from the spread of
  the paired differences across rounds. Observation-level precision within one
  result and variation between points or rounds remain distinct evidence; a
  method may combine them only through a declared rule. When there is not enough
  evidence to estimate noise, the default verdict is indeterminate with reason
  `insufficient-history`. A method may instead declare a provisional rule, such
  as a project-supplied noise level for new series; how such a rule is chosen is
  left to the method and project, and the output identifies the source of the
  estimate and whether it is provisional.
- **Verdict**: the initial method indicates a change when |effect| exceeds *k* ×
  noise, with *k* a project-configured multiplier. Indicated changes are labeled
  improved or regressed using the quantity's direction metadata when present,
  otherwise just *changed*. When the evidence is sufficient but the threshold
  is not exceeded, the verdict is *no change detected*.
- **Indeterminate**: there is no usable target or reference, ordering is unknown,
  or the method lacks enough evidence and no provisional rule applies. The
  reason is part of the verdict.

A quantity declared deterministic (§4.4 of the schema) does not need a noise
estimate. Once it has an eligible reference, exact equality means no change
detected and any difference indicates a change.

The dispersion-scaled initial method is deliberately simple and respects each
series' observed variability — a 2% change on a 0.2%-noise series is indicated,
while a 2% change on a 5%-noise series is not. Prediction intervals,
significance tests, paired confidence intervals, minimum-effect filters, or
multiple-comparison control can be added as new method versions without
changing the surrounding contract.

## 5. Output

A **comparison document** is machine-readable and self-describing. For each
evaluation it records:

- the mode, and the target and exact reference inputs: reference points in
  history mode; the `run_key`, profile, varying coordinate, sides, and pairing
  in run mode;
- the series fingerprint, identity-policy schema, estimator, and ordering or
  ancestry used;
- the reference window, reset or continuity boundaries, comparison method and
  version, and complete configuration;
- the reference estimate, effect measure and value, noise and its source,
  threshold or limits, evidence counts, and verdict with reason; and
- exclusions, failed profile invariants, observed-context differences, quality
  caveats, and missing target results from a batch request.

The document is renderable as a reviewer-speed summary ("these target results
regressed, these improved, these show no detected change, these were
indeterminate, and these were missing") and consumable by alerting or CI gates.

Comparison documents are derived and recomputed on demand; the result store
persists their immutable inputs rather than the documents themselves. Supplying
the recorded inputs, method version, and configuration reproduces the document
even after newer points arrive.

## 6. Boundaries

- **Not target selection:** selecting "latest", a target run, or a target
  revision happens before invoking the comparator.
- **Not an arbitrary cross-series pair engine:** history mode evaluates a target
  against preceding points in the same series; run mode compares only sides
  that a profile defines within one run.
- **Not policy:** *k*, provisional rules, eligibility rules, and gating
  decisions are project configuration; the comparator reports, humans and
  policy decide.
- **Not the detector:** it consumes a fixed historical reference but does not
  watch series, schedule evaluation, detect drift, or locate change points.
- **Not the identity authority:** series membership comes from the store's
  identity policy; the comparator only validates and consumes it.
- **Not a data source:** every number in the output is traceable to input results.

## 7. Open questions

1. **Run-mode default method.** UC-03's sketch reports a ratio with a 95%
   confidence interval over paired rounds. Should that be the initial run-mode
   method, with the dispersion-scaled rule kept for history mode, or should
   both modes start from the same *k* × noise rule?
2. **Unplanned run-mode comparisons.** A run made without alternating sides
   fails the profile's interleaving invariant. Should the comparator refuse it,
   or allow an explicitly unpaired method whose output is marked as such?
