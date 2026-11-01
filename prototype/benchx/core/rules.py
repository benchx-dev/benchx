"""Rules JSON Schema cannot express. They assume the document fits its schema.

They hold for the document alone: what one runner or machine cannot carry out
is that runner's refusal, not a rule.
"""

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any, assert_never

from .errors import (
    CompletedExceedsAttempted,
    DuplicateEstimate,
    EmptyInterval,
    EndedBeforeStarted,
    PathParts,
    PrimarySourceMismatch,
    RuleViolation,
    SharedParameterName,
)
from .validation import Kind


def check(data: dict[str, Any], kind: Kind) -> Iterator[RuleViolation]:
    if kind == "measurement-result":
        yield from check_result(data)
    elif kind == "work-order":
        yield from check_order(data)
    elif (
        kind == "comparison-document"
        or kind == "target-description"
        or kind == "target-provider-request"
        or kind == "config"
    ):
        return  # nothing beyond what their schemas say
    else:
        assert_never(kind)


def check_result(data: dict[str, Any]) -> Iterator[RuleViolation]:
    for rule in [
        primary_source,
        one_estimate_per_estimator,
        ordered_bounds,
        ordered_timestamps,
        repetition_counts,
    ]:
        yield from rule(data)


def primary_source(data: dict[str, Any]) -> Iterator[RuleViolation]:
    expected = data["source"]["uri"]
    components = data["coordinates"]["subject"]["components"]

    for index, component in enumerate(components):
        if component["role"] != "primary":
            continue

        reported = component.get("source")
        if reported is None or reported == expected:
            continue

        yield PrimarySourceMismatch(
            path=("coordinates", "subject", "components", index, "source"),
            reported=reported,
            expected=expected,
        )


def one_estimate_per_estimator(data: dict[str, Any]) -> Iterator[RuleViolation]:
    summaries = data["measurement"].get("summaries", [])
    seen: set[str] = set()

    for index, summary in enumerate(summaries):
        if summary["type"] != "estimate":
            continue

        estimator = summary["estimator"]
        key = json.dumps(estimator, sort_keys=True)
        if key not in seen:
            seen.add(key)
            continue

        yield DuplicateEstimate(
            path=("measurement", "summaries", index),
            estimator=estimator["name"],
        )


def ordered_bounds(data: dict[str, Any]) -> Iterator[RuleViolation]:
    measurement = data["measurement"]

    constraint = measurement.get("constraint")
    if constraint is not None and constraint["kind"] == "interval":
        yield from _empty_interval(
            path=("measurement", "constraint"),
            lower=constraint["lower"],
            upper=constraint["upper"],
            closed=constraint["lower_inclusive"] and constraint["upper_inclusive"],
        )

    for index, summary in enumerate(measurement.get("summaries", [])):
        if "lower" not in summary or "upper" not in summary:
            continue

        yield from _empty_interval(
            path=("measurement", "summaries", index),
            lower=summary["lower"],
            upper=summary["upper"],
            closed=True,
        )


def _empty_interval(
    path: PathParts, lower: float, upper: float, closed: bool
) -> Iterator[RuleViolation]:
    if lower > upper or (lower == upper and not closed):
        yield EmptyInterval(path=path, lower=lower, upper=upper)


def ordered_timestamps(data: dict[str, Any]) -> Iterator[RuleViolation]:
    provenance = data["provenance"]
    if "ended_at" not in provenance:
        return

    started = datetime.fromisoformat(provenance["started_at"])
    ended = datetime.fromisoformat(provenance["ended_at"])
    if ended < started:
        yield EndedBeforeStarted(path=("provenance", "ended_at"))


def repetition_counts(data: dict[str, Any]) -> Iterator[RuleViolation]:
    procedure = data.get("procedure", {})
    attempted = procedure.get("attempted_repetitions")
    completed = procedure.get("completed_repetitions")
    if attempted is None or completed is None:
        return

    if completed > attempted:
        yield CompletedExceedsAttempted(
            path=("procedure", "completed_repetitions"),
            completed=completed,
            attempted=attempted,
        )


def check_order(data: dict[str, Any]) -> Iterator[RuleViolation]:
    yield from distinct_parameter_names(data)


def distinct_parameter_names(data: dict[str, Any]) -> Iterator[RuleViolation]:
    variables = data.get("environment_variables", {})
    for name in data.get("workload_parameters", {}):
        if name in variables:
            yield SharedParameterName(path=("workload_parameters", name), name=name)
