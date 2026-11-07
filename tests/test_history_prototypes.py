"""History mode and detectors, as test-only prototypes (comparator.md §2.2, §2.5, §4.6, §8).

PROTOTYPES, NOT A FEATURE. The selectors, methods, and detector here exist to check
that the types in benchx.evidence and comparison-document/0.1.0 can express history
mode. Nothing here is reachable from the CLI or from configuration, and the
change-point detector is deliberately trivial.

The worked examples of comparator.md §8 are test cases, so the document cannot
drift from what the code computes. Their values are constructed, not measured.
"""

import math
import statistics
from dataclasses import dataclass

import pytest

from benchx import compare, core, identity
from benchx.evidence import (COLUMNS, Assessment, CompareError, Evidence, Method, Row, Selection, Selector, change_label,
                             result_ref)
from benchx.projection import Projection, estimate, values
from benchx.run_profile import RUN_PROFILE
from benchx.store import store_project
from test_comparator import BASE, HEAD, result

# --- the prototypes -----------------------------------------------------------


def _row(doc, side) -> Row:
    batch = values(doc)
    return Row(result_ref(doc), side, {name: key.read(doc) for name, key in COLUMNS.items()},
               estimate(identity.MEDIAN, batch), tuple(batch))


def _series_of(doc, projection) -> str:
    return projection.series_fingerprint(projection.apply(doc, store_project(doc)),
                                         doc["measurement"]["quantity"]["name"], identity.MEDIAN)


def _selection(selector, params, projection, documents, unit) -> Selection:
    return Selection(selector={"name": selector.name, "version": selector.version, "params": params},
                     estimator=dict(identity.MEDIAN), identity_schema=projection.identity_schema, local_only=False,
                     inputs=[result_ref(d) for d in documents], units=[unit])


def _evidence(doc, projection, sides, rows, **history) -> Evidence:
    workload = doc["coordinates"]["workload"]
    return Evidence(_series_of(doc, projection), {"name": workload["name"], "parameters": workload["parameters"]},
                    doc["measurement"]["quantity"], sides, tuple(rows), ordering="native-time", **history)


class PrecedingWindow(Selector):
    """Comparator.md §2.2: an explicit target against the points before it in its
    series, stopping at the last boundary. Ordered by measurement start, since the
    implementation has no revision graph."""
    name, version = "test/preceding-window", "v0"

    def select(self, documents, *, target, window, boundaries=(), projection=identity.POLICY):
        documents = sorted(documents, key=lambda d: (d["provenance"]["started_at"], d["ingest_key"]))
        chosen = next(d for d in documents if result_ref(d) == target)
        if any(_series_of(d, projection) != _series_of(chosen, projection) for d in documents):
            raise CompareError("every point must lie in the target's series")
        before = documents[:documents.index(chosen)]
        for boundary in boundaries:
            starts = [i for i, d in enumerate(before) if result_ref(d) == boundary["at"]]
            if boundary["kind"] in ("change-point", "distribution-reset") and starts:
                before = before[starts[0]:]
        before = before[-window:]
        used = [*before, chosen]
        rows = [*(_row(d, "reference") for d in before), _row(chosen, "target")]
        unit = _evidence(chosen, projection, ("reference", "target"), rows, target=(target,),
                         reference=tuple(result_ref(d) for d in before), boundaries=tuple(boundaries))
        return _selection(self, {"target": target, "window": window}, projection, used, unit)


class LatestPoint(Method):
    """Comparator.md §4.3: one new point against the spread of the reference points.
    `noise_kind` exists only to show that the choice decides the verdict."""
    name, version, dims = "test/latest-point", "v0", ("side",)
    defaults = {"k": 3.0, "min_points": 4, "noise_kind": "standard-deviation"}
    details_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["points", "noise_source"],
        "properties": {
            "points": {"type": "integer"}, "reference_estimate": {"type": "number"}, "noise": {"type": "number"},
            "noise_kind": {"enum": ["standard-deviation", "standard-error"]},
            "noise_source": {"const": "reference-points"}, "threshold": {"type": "number"},
            "degrees_of_freedom": {"type": "integer"},
        },
    }

    def assess(self, layout, evidence, config):
        reference, target = layout.cells
        used = (*reference, *target)
        details = {"points": len(reference), "noise_source": "reference-points"}
        revisions = [row.columns["revision"] for row in reference]
        if len(set(revisions)) != len(revisions):
            return Assessment("indeterminate", reason="duplicate-points", used=used, details=details)
        if len(reference) < config["min_points"]:
            return Assessment("indeterminate", reason="insufficient-history", used=used, details=details)
        points = [row.estimate for row in reference]
        center = statistics.median(points)
        noise = statistics.stdev(points) / center
        if config["noise_kind"] == "standard-error":
            noise /= math.sqrt(len(points))
        effect = target[0].estimate / center - 1
        threshold = config["k"] * noise
        details.update(reference_estimate=center, noise=noise, noise_kind=config["noise_kind"], threshold=threshold,
                       degrees_of_freedom=len(points) - 1)
        verdict = ("no-change-detected" if abs(effect) <= threshold
                   else change_label(effect, evidence.quantity.get("direction")))
        return Assessment(verdict, effect={"measure": "relative", "value": effect}, used=used, details=details)


class AnchoredWindow(Selector):
    """Comparator.md §2.2: the subject and a fixed build, told apart by a label and
    measured each night. The sides are the build kinds, and the projection keeps
    both in one series with the label on its axis."""
    name, version = "test/anchored-window", "v0"
    PROJECTION = Projection(identity.POLICY_DATA, ["/provenance/labels/kind", "/revision/key"],
                            profile="test/profile/anchored/v0")

    def select(self, documents, *, target_run):
        documents = sorted(documents, key=lambda d: (d["provenance"]["started_at"], d["ingest_key"]))
        rows = [_row(d, d["provenance"]["labels"]["kind"]) for d in documents]
        tonight = [d for d in documents if d["provenance"]["run_key"] == target_run]
        before = [d for d in documents if d["provenance"]["run_key"] != target_run]
        subject = next(d for d in tonight if d["provenance"]["labels"]["kind"] == "subject")
        unit = _evidence(subject, self.PROJECTION, ("anchor", "subject"), rows,
                         target=tuple(result_ref(d) for d in tonight), reference=tuple(result_ref(d) for d in before))
        return _selection(self, {"target_run": target_run}, self.PROJECTION, documents, unit)


class AnchoredRatio(Method):
    """The target night's ratio of subject to anchor, against the same ratio on
    earlier nights. Each night is a block, so its machine state cancels."""
    name, version, dims = "test/anchored-ratio", "v0", ("run_key", "side")
    requires = ("duplicate-points",)
    defaults = {"k": 3.0}

    def assess(self, layout, evidence, config):
        tonight = {result["ingest_key"] for result in evidence.target}
        ratios, target = [], None
        for anchor, subject in layout.cells:  # one per night, the sides in evidence order
            ratio = subject[0].estimate / anchor[0].estimate
            if subject[0].result["ingest_key"] in tonight:
                target = ratio
            else:
                ratios.append(ratio)
        center = statistics.median(ratios)
        noise = statistics.stdev(ratios) / center
        effect = target / center - 1
        threshold = config["k"] * noise
        verdict = ("no-change-detected" if abs(effect) <= threshold
                   else change_label(effect, evidence.quantity.get("direction")))
        return Assessment(verdict, effect={"measure": "relative", "value": effect},
                          used=tuple(r for leaf in layout.leaves() for r in leaf),
                          details={"noise": noise, "noise_kind": "standard-deviation", "threshold": threshold,
                                   "nights": len(ratios)})


class Buckets(Method):
    """What a run looks like without its blocks: two buckets of attempts, the
    worked example's foil to pairing (comparator.md §8.1)."""
    name, version, dims = "test/buckets", "v0", ("side",)
    defaults = {"k": 3.0}

    def assess(self, layout, evidence, config):
        baseline, contender = ([row.estimate for row in side] for side in layout.cells)
        center = statistics.fmean(baseline)
        effect = statistics.fmean(contender) / center - 1
        noise = math.sqrt((statistics.variance(baseline) + statistics.variance(contender)) / len(baseline)) / center
        threshold = config["k"] * noise
        verdict = ("no-change-detected" if abs(effect) <= threshold
                   else change_label(effect, evidence.quantity.get("direction")))
        return Assessment(verdict, effect={"measure": "relative", "value": effect},
                          used=tuple(r for leaf in layout.leaves() for r in leaf),
                          details={"noise": noise, "threshold": threshold})


class Explicit(Selector):
    """Comparator.md §2.5: the caller lists the points; they must still lie in one series."""
    name, version = "test/explicit", "v0"

    def select(self, documents, *, reference, target, calibration=(), projection=identity.POLICY):
        by_ref = {(d["producer"]["name"], d["ingest_key"]): d for d in documents}
        chosen = [by_ref[(reference["producer"], reference["ingest_key"])],
                  by_ref[(target["producer"], target["ingest_key"])]]
        if _series_of(chosen[0], projection) != _series_of(chosen[1], projection):
            raise CompareError("the points must lie in one series")
        unit = _evidence(chosen[1], projection, ("reference", "target"),
                         [_row(chosen[0], "reference"), _row(chosen[1], "target")], target=(target,),
                         reference=(reference,), calibration=tuple(calibration))
        return _selection(self, {"reference": reference, "target": target}, projection, chosen, unit)


class Calibrated(Method):
    """One point per side has no spread, so the threshold comes from past
    comparisons in the series (calibration history), as rustc-perf does."""
    name, version, dims = "test/calibrated", "v0", ("side",)
    defaults = {"k": 3.0, "min_comparisons": 4}

    def assess(self, layout, evidence, config):
        reference, target = layout.cells
        used = (*reference, *target)
        if len(evidence.calibration) < config["min_comparisons"]:
            return Assessment("indeterminate", reason="insufficient-history", used=used)
        effect = target[0].estimate / reference[0].estimate - 1
        threshold = config["k"] * statistics.stdev(evidence.calibration)
        verdict = ("no-change-detected" if abs(effect) <= threshold
                   else change_label(effect, evidence.quantity.get("direction")))
        return Assessment(verdict, effect={"measure": "relative", "value": effect}, used=used,
                          details={"threshold": threshold, "noise_source": "calibration-history"})


@dataclass(frozen=True)
class Finding:
    boundary: dict
    assessment: Assessment


class StepDetector:
    """Comparator.md §4.6, and deliberately trivial: a step in the medians either
    side of each point, placed where it is largest. It stands in for a real
    algorithm to check the shape."""

    def __init__(self, margin=0.05, span=4):
        self.margin, self.span = margin, span

    def scan(self, series: list[tuple[dict, float]]) -> list[Finding]:
        steps = {}
        for i in range(self.span, len(series) - self.span + 1):
            before = statistics.median(v for _, v in series[i - self.span:i])
            after = statistics.median(v for _, v in series[i:i + self.span])
            steps[i] = after / before - 1
        findings, run = [], []
        for i in [*steps, None]:  # a window straddling a step exceeds the margin too: keep each run's peak
            if i is not None and abs(steps[i]) > self.margin:
                run.append(i)
            elif run:
                peak = max(run, key=lambda j: abs(steps[j]))
                findings.append(Finding({"kind": "change-point", "at": series[peak][0],
                                         "detail": f"step of {steps[peak]:+.1%}"},
                                        Assessment(change_label(steps[peak], "lower-is-better"),
                                                   effect={"measure": "relative", "value": steps[peak]})))
                run = []
        return findings


# --- one series: NumPy sort, float64, 10^6 (constructed values) ------------------

def sort_result(revision, run_key, day, value, kind="subject", number=0):
    doc = result(revision, 0, 0, value, workload="sort")
    doc["coordinates"]["workload"]["parameters"] = {"dtype": "float64", "n": 1000000}
    doc["ingest_key"] = f"{run_key}:{kind}:{number}"
    doc["provenance"].update(run_key=run_key, started_at=f"2026-{10 + day // 28:02}-{1 + day % 28:02}T02:00:00Z",
                             labels={"kind": kind})
    return doc


NIGHTS = [41.3, 39.7, 42.4, 40.1, 43.0, 40.9, 38.9, 41.8, 39.2, 42.9, 40.4, 41.1]
LATEST = statistics.median(NIGHTS) * 1.09
# Within a night, the subject over the anchor: what is left when the night cancels.
WOBBLE = [1.004, 0.997, 1.006, 0.993, 1.002, 0.998, 1.007, 0.995, 1.001, 0.996, 1.005, 0.999]


def subject_nights() -> list[dict]:
    docs = [sort_result(f"{i:040x}", f"night-{i:02}", i, value) for i, value in enumerate(NIGHTS)]
    docs.append(sort_result(f"{12:040x}", "night-12", 12, LATEST))
    return docs


def history_document(docs, config=None, boundaries=()):
    selection = PrecedingWindow().select(docs, target=result_ref(docs[-1]), window=12, boundaries=boundaries)
    doc = compare.evaluate(selection, LatestPoint(), {**LatestPoint.defaults, **(config or {})})
    assert core.check(doc, "comparison-document") == []
    return doc


# --- §8.1 run mode: paired by round ------------------------------------------------

DRIFT = [1.00, 1.06, 0.95, 1.03, 0.92, 1.08, 0.97, 1.04, 0.94, 1.01]
PAIR = [1.021, 1.012, 1.028, 1.016, 1.025, 1.010, 1.030, 1.019, 1.014, 1.024]
BASE_ROUNDS = [41.0 * d for d in DRIFT]
HEAD_ROUNDS = [b * p for b, p in zip(BASE_ROUNDS, PAIR)]


def paired_run() -> list[dict]:
    docs, slot = [], 0
    for r, (b, h) in enumerate(zip(BASE_ROUNDS, HEAD_ROUNDS)):
        for side, value in ((BASE, b), (HEAD, h)):
            docs.append(result(side, r, slot, value, workload="sort"))
            slot += 1
    return docs


def test_8_1_pairing_detects_what_two_buckets_hide():
    paired = compare.compare(paired_run(), run_key="run", profile="revisions", baseline=BASE)["units"][0]
    selection = RUN_PROFILE.select(paired_run(), run_key="run", profile="revisions", baseline=BASE)
    document = compare.evaluate(selection, Buckets(), Buckets.defaults)
    assert core.check(document, "comparison-document") == []
    buckets = document["units"][0]

    assert paired["verdict"] == "regressed" and buckets["verdict"] == "no-change-detected"
    assert paired["effect"]["value"] == pytest.approx(0.0199, abs=5e-5)
    assert paired["details"]["noise"] == pytest.approx(0.0022, abs=5e-5)
    assert paired["details"]["threshold"] == pytest.approx(0.0065, abs=5e-5)
    assert buckets["effect"]["value"] == pytest.approx(0.0197, abs=5e-5)
    assert buckets["details"]["noise"] == pytest.approx(0.0233, abs=5e-5)
    assert buckets["details"]["threshold"] == pytest.approx(0.0700, abs=5e-5)
    assert statistics.stdev(BASE_ROUNDS) / statistics.fmean(BASE_ROUNDS) == pytest.approx(0.054, abs=5e-4)
    assert statistics.stdev(PAIR) == pytest.approx(0.007, abs=5e-4)
    # The same results, the same method framework: only the dims differ.
    assert paired["evidence"]["keys"][0]["relation"] == "crossed" and buckets["evidence"]["keys"] == []


def _standard_errors(base, head):
    """(paired, two buckets) for each effect measure."""
    n, mean_b, mean_h = len(base), statistics.fmean(base), statistics.fmean(head)
    var_b, var_h = statistics.variance(base), statistics.variance(head)
    root_n = math.sqrt(n)
    log_b, log_h = [math.log(v) for v in base], [math.log(v) for v in head]
    ratio = mean_h / mean_b
    return {
        "difference": (statistics.stdev(h - b for b, h in zip(base, head)) / root_n, math.sqrt((var_b + var_h) / n)),
        "relative": (statistics.stdev(h / b - 1 for b, h in zip(base, head)) / root_n,
                     math.sqrt((var_b + var_h) / n) / mean_b),
        "log-ratio": (statistics.stdev(h - b for b, h in zip(log_b, log_h)) / root_n,
                      math.sqrt((statistics.variance(log_b) + statistics.variance(log_h)) / n)),
        "ratio-of-means": (statistics.stdev(h - ratio * b for b, h in zip(base, head)) / (root_n * mean_b),
                           math.sqrt(var_h + ratio ** 2 * var_b) / (root_n * mean_b)),
    }


@pytest.mark.parametrize("measure, gain", [("difference", 11.8), ("relative", 10.8), ("log-ratio", 10.9),
                                           ("ratio-of-means", 10.8)])
def test_8_1_pairing_cuts_the_standard_error_whichever_the_effect_measure(measure, gain):
    paired, buckets = _standard_errors(BASE_ROUNDS, HEAD_ROUNDS)[measure]
    assert buckets / paired == pytest.approx(gain, abs=0.05)


@pytest.mark.parametrize("shared, own, gain", [(0.05, 0.007, 10.2), (0.05, 0.05, 1.7), (0.0, 0.01, 1.0)])
def test_8_1_the_gain_comes_from_the_data_not_from_pairing(shared, own, gain):
    """Comparator.md §8.1's formula, against a seeded simulation: when rounds share no
    drift, pairing gains nothing."""
    import random
    rng = random.Random(1)
    n = 2000
    base, head = [], []
    for _ in range(n):
        drift = rng.gauss(0, shared)
        b = 40 * (1 + drift + rng.gauss(0, own / math.sqrt(2)))
        base.append(b)
        head.append(40 * (1 + drift + rng.gauss(0, own / math.sqrt(2))))
    paired, buckets = _standard_errors(base, head)["difference"]
    assert math.sqrt(2 * shared ** 2 + own ** 2) / own == pytest.approx(gain, abs=0.05)
    assert buckets / paired == pytest.approx(gain, rel=0.1)


def test_8_1_an_unpaired_rank_test_gives_up_the_same_gain():
    """Comparator.md §4.5: asking less of the run costs sensitivity on this run."""
    doc = compare.compare(paired_run(), run_key="run", profile="revisions", baseline=BASE, method="benchx/mann-whitney/v1")
    assert doc["units"][0]["verdict"] == "no-change-detected"


# --- §8.2 history mode: the latest point against 12 ancestors ------------------------

def test_8_2_spread_between_nights_hides_a_nine_percent_regression():
    unit = history_document(subject_nights())["units"][0]
    assert unit["verdict"] == "no-change-detected"
    assert unit["effect"]["value"] == pytest.approx(0.09)
    assert unit["details"]["noise"] == pytest.approx(0.0336, abs=5e-5)
    assert unit["details"]["threshold"] == pytest.approx(0.1007, abs=5e-5)
    assert unit["details"]["noise_kind"] == "standard-deviation" and unit["details"]["degrees_of_freedom"] == 11
    assert unit["details"]["reference_estimate"] == 41.0
    assert len(unit["evidence"]["reference"]) == 12 and unit["evidence"]["ordering"] == "native-time"
    assert unit["used"] == [*unit["evidence"]["reference"], *unit["evidence"]["target"]]


def test_8_2_what_noise_means_decides_the_verdict():
    """Comparator.md §4.3: the standard error detects it, and half the ordinary nights too."""
    unit = history_document(subject_nights(), {"noise_kind": "standard-error"})["units"][0]
    assert unit["verdict"] == "regressed" and unit["details"]["threshold"] == pytest.approx(0.0291, abs=5e-5)
    ordinary = [v for v in NIGHTS if abs(v / 41.0 - 1) > unit["details"]["threshold"]]
    assert len(ordinary) == 6


def test_history_needs_enough_points():
    docs = subject_nights()[-3:]
    unit = history_document(docs)["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-history")


def test_history_refuses_a_revision_with_two_points():
    """Comparator.md §3: the framework's cell check cannot see this, so the method reads `revision`."""
    docs = subject_nights()
    again = sort_result(docs[3]["revision"]["key"], "night-03b", 3, 40.0, number=1)
    unit = history_document(sorted([*docs, again], key=lambda d: d["provenance"]["started_at"]))["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "duplicate-points")


def test_history_refuses_a_point_from_another_series():
    docs = subject_nights()
    docs[0]["coordinates"]["subject"]["configuration"] = {"build_type": "Debug"}
    with pytest.raises(CompareError, match="series"):
        PrecedingWindow().select(docs, target=result_ref(docs[-1]), window=12)


def test_history_keeps_a_stable_verdict_when_newer_results_arrive():
    docs = subject_nights()
    target = result_ref(docs[-1])
    later = sort_result(f"{13:040x}", "night-13", 13, 60.0)
    selector, method = PrecedingWindow(), LatestPoint()
    now = compare.evaluate(selector.select(docs, target=target, window=12), method, LatestPoint.defaults)
    then = compare.evaluate(selector.select([*docs, later], target=target, window=12), method, LatestPoint.defaults)
    assert now == then


# --- §8.3 anchored history: a fixed build measured each night ------------------------

RELEASE = "a" * 40


def anchored_nights() -> list[dict]:
    docs = []
    for i, (value, wobble) in enumerate(zip(NIGHTS, WOBBLE)):
        docs.append(sort_result(f"{i:040x}", f"night-{i:02}", i, value))
        docs.append(sort_result(RELEASE, f"night-{i:02}", i, value / wobble, kind="anchor"))
    ratios = sorted(WOBBLE)
    center = (ratios[5] + ratios[6]) / 2
    docs.append(sort_result(f"{12:040x}", "night-12", 12, 41.0 * center * 1.09))
    docs.append(sort_result(RELEASE, "night-12", 12, 41.0, kind="anchor"))
    return docs


def anchored_document():
    selection = AnchoredWindow().select(anchored_nights(), target_run="night-12")
    doc = compare.evaluate(selection, AnchoredRatio(), AnchoredRatio.defaults)
    assert core.check(doc, "comparison-document") == []
    return doc


def test_8_3_each_night_is_a_block_and_the_regression_shows():
    unit = anchored_document()["units"][0]
    assert unit["verdict"] == "regressed" and unit["effect"]["value"] == pytest.approx(0.09)
    assert unit["details"]["noise"] == pytest.approx(0.0046, abs=5e-5)
    assert unit["details"]["threshold"] == pytest.approx(0.014, abs=5e-4)
    assert unit["details"]["nights"] == 12


def test_8_3_counting_finds_the_block_with_nothing_told_about_anchoring():
    [key] = anchored_document()["units"][0]["evidence"]["keys"]
    assert (key["name"], key["relation"], key["complete"]) == ("run_key", "crossed", True)
    assert key["key"] == {"pointer": "/provenance/run_key", "order": "natural"}


def test_8_3_a_night_with_one_build_missing_is_masked():
    docs = anchored_nights()
    docs = [d for d in docs if result_ref(d)["ingest_key"] != "night-04:anchor:0"]
    unit = AnchoredWindow().select(docs, target_run="night-12").units[0]
    from benchx.evidence import is_complete, pivot, relation
    assert relation(unit.rows, "run_key") == "crossed" and not is_complete(unit.rows, "run_key", unit.sides)
    cells = pivot(unit, ("run_key", "side")).cells
    assert sum(1 for anchor, subject in cells if not anchor) == 1


def test_the_anchor_is_in_the_subjects_series_unless_something_tells_them_apart():
    """Comparator.md §2.2: under the identity policy the revision is the axis and
    nothing else differs, so a window that does not look at the label would take
    anchor measurements for history. This is the hazard the anchored selector avoids."""
    subject = sort_result(f"{1:040x}", "night-01", 1, 41.0)
    anchor = sort_result(RELEASE, "night-01", 1, 41.0, kind="anchor")
    assert _series_of(subject, identity.POLICY) == _series_of(anchor, identity.POLICY)
    mixed = PrecedingWindow().select([anchor, subject], target=result_ref(subject), window=12)
    assert mixed.units[0].reference == (result_ref(anchor),)


# --- §2.5 explicit: one side against one side across runs -----------------------------

def _releases(previous=41.0, current=44.69):
    return [sort_result(f"{1:040x}", "release-1", 1, previous), sort_result(f"{2:040x}", "release-2", 40, current)]


def test_a_release_against_the_previous_release_uses_calibration_history():
    docs = _releases()
    selection = Explicit().select(docs, reference=result_ref(docs[0]), target=result_ref(docs[1]),
                                  calibration=(0.002, -0.004, 0.003, -0.001, 0.005))
    doc = compare.evaluate(selection, Calibrated(), Calibrated.defaults)
    assert core.check(doc, "comparison-document") == []
    assert doc["units"][0]["verdict"] == "regressed" and doc["units"][0]["details"]["noise_source"] == "calibration-history"


def test_one_point_per_side_with_no_calibration_is_not_a_verdict():
    docs = _releases()
    selection = Explicit().select(docs, reference=result_ref(docs[0]), target=result_ref(docs[1]))
    unit = compare.evaluate(selection, Calibrated(), Calibrated.defaults)["units"][0]
    assert (unit["verdict"], unit["reason"]) == ("indeterminate", "insufficient-history")


def test_explicit_points_must_lie_in_one_series():
    docs = _releases()
    docs[1]["coordinates"]["subject"]["configuration"] = {"build_type": "Debug"}
    with pytest.raises(CompareError, match="one series"):
        Explicit().select(docs, reference=result_ref(docs[0]), target=result_ref(docs[1]))


# --- §4.6 a detector, whose accepted findings end a reference --------------------------

def stepped() -> list[dict]:
    """A real 15% step at night 8, then quiet nights, then a quiet target."""
    quiet = [41.0, 41.2, 40.8, 41.1, 40.9, 41.3, 40.7, 41.0]
    after = [47.1, 47.3, 46.9, 47.2, 47.0, 47.4, 46.8, 47.1]
    docs = [sort_result(f"{i:040x}", f"night-{i:02}", i, v) for i, v in enumerate([*quiet, *after])]
    docs.append(sort_result(f"{16:040x}", "night-16", 16, 47.0))
    return docs


def test_a_detector_finds_a_step_as_a_boundary_and_an_assessment():
    docs = stepped()
    findings = StepDetector().scan([(result_ref(d), d["measurement"]["observations"][0]) for d in docs])
    assert [f.boundary["kind"] for f in findings] == ["change-point"]
    assert findings[0].boundary["at"]["ingest_key"] == "night-08:subject:0"
    assert findings[0].assessment.verdict == "regressed"
    assert findings[0].assessment.effect["value"] == pytest.approx(0.15, abs=0.01)


def test_a_quiet_series_has_no_findings():
    series = [({"producer": "p", "ingest_key": str(i)}, v) for i, v in enumerate(NIGHTS)]
    assert StepDetector(margin=0.1).scan(series) == []


def test_an_accepted_finding_ends_the_reference_so_the_old_regime_stops_inflating_noise():
    docs = stepped()
    target = result_ref(docs[-1])
    selector, method = PrecedingWindow(), LatestPoint()
    before = compare.evaluate(selector.select(docs, target=target, window=20), method, LatestPoint.defaults)["units"][0]
    finding, = StepDetector().scan([(result_ref(d), d["measurement"]["observations"][0]) for d in docs])
    doc = compare.evaluate(selector.select(docs, target=target, window=20, boundaries=(finding.boundary,)), method,
                           LatestPoint.defaults)
    assert core.check(doc, "comparison-document") == []
    after = doc["units"][0]
    assert before["details"]["points"] == 16 and after["details"]["points"] == 8
    assert after["details"]["noise"] < before["details"]["noise"] / 5
    assert after["evidence"]["boundaries"] == [finding.boundary]
    assert after["evidence"]["reference"][0]["ingest_key"] == "night-08:subject:0"
