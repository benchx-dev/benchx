import copy

import pytest

from benchx.core import rules, validation
from benchx.core.errors import (
    Code,
    CompletedExceedsAttempted,
    DuplicateEstimate,
    EmptyInterval,
    EndedBeforeStarted,
    PrimarySourceMismatch,
    RuleViolation,
    SharedParameterName,
)


def check(data: dict) -> list[RuleViolation]:
    validator, _ = validation.find_schema(data, "measurement-result")
    assert list(validation.check(data, validator)) == []
    return list(rules.check(data, "measurement-result"))


def where(issues: list[RuleViolation]) -> list[tuple]:
    return [(type(issue), issue.path) for issue in issues]


def test_examples_break_no_rules(example):
    assert check(example) == []


def test_well_formed_order_breaks_no_rules():
    document = {
        "environment_variables": {"OMP_NUM_THREADS": "1"},
        "workload_parameters": {"size": 10},
    }
    assert list(rules.check(document, "work-order")) == []


def test_name_shared_by_environment_and_parameters():
    document = {
        "environment_variables": {"threads": "1", "OMP_NUM_THREADS": "1"},
        "workload_parameters": {"threads": 1, "size": 10},
    }
    [issue] = rules.check(document, "work-order")
    assert issue == SharedParameterName(
        path=("workload_parameters", "threads"), name="threads"
    )
    assert issue.message == (
        "'threads' is both an environment variable and a workload parameter"
    )


def test_rules_not_implemented():
    with pytest.raises(NotImplementedError, match="comparison-document"):
        list(rules.check({}, "comparison-document"))


def test_primary_source_mismatch(adhoc):
    adhoc["coordinates"]["subject"]["components"][0]["source"] = (
        "https://github.com/someone/fork"
    )
    [issue] = check(adhoc)
    assert issue == PrimarySourceMismatch(
        path=("coordinates", "subject", "components", 0, "source"),
        reported="https://github.com/someone/fork",
        expected=adhoc["source"]["uri"],
    )
    assert issue.code is Code.IDENTITY_VIOLATION
    assert issue.rule == "primary-source"


def test_primary_source_match_is_fine(adhoc):
    adhoc["coordinates"]["subject"]["components"][0]["source"] = adhoc["source"]["uri"]
    assert check(adhoc) == []


def test_duplicate_estimate(arrow):
    arrow["measurement"]["summaries"].append(
        copy.deepcopy(arrow["measurement"]["summaries"][0])
    )
    [issue] = check(arrow)
    assert type(issue) is DuplicateEstimate and issue.code is Code.MALFORMED
    assert issue.path == ("measurement", "summaries", 3)


def test_every_duplicate_estimate_is_reported(arrow):
    first = arrow["measurement"]["summaries"][0]
    arrow["measurement"]["summaries"] += [copy.deepcopy(first), copy.deepcopy(first)]
    assert where(check(arrow)) == [
        (DuplicateEstimate, ("measurement", "summaries", 3)),
        (DuplicateEstimate, ("measurement", "summaries", 4)),
    ]


def test_different_method_versions_are_different_estimators(arrow):
    other = copy.deepcopy(arrow["measurement"]["summaries"][0])
    other["estimator"]["method_version"] = "benchx/mean/v2"
    arrow["measurement"]["summaries"].append(other)
    assert check(arrow) == []


def test_inverted_confidence_interval(arrow):
    arrow["measurement"]["summaries"][2].update(lower=0.04, upper=0.03)
    [issue] = check(arrow)
    assert issue == EmptyInterval(
        path=("measurement", "summaries", 2), lower=0.04, upper=0.03
    )


@pytest.mark.parametrize(
    "lower, upper, inclusive, ok",
    [
        (1, 2, False, True),
        (2, 2, True, True),
        (2, 2, False, False),
        (3, 2, True, False),
    ],
)
def test_censored_interval(adhoc, lower, upper, inclusive, ok):
    adhoc["measurement"] = {
        "status": "censored",
        "reason": "timeout",
        "constraint": {
            "kind": "interval",
            "lower": lower,
            "upper": upper,
            "lower_inclusive": inclusive,
            "upper_inclusive": inclusive,
            "cause": "timeout",
        },
    }
    assert (check(adhoc) == []) is ok


def test_ended_before_started(adhoc):
    adhoc["provenance"]["ended_at"] = "2026-09-14T10:00:00Z"
    assert where(check(adhoc)) == [(EndedBeforeStarted, ("provenance", "ended_at"))]


def test_completed_exceeds_attempted(adhoc):
    adhoc["procedure"]["completed_repetitions"] = 6
    [issue] = check(adhoc)
    assert type(issue) is CompletedExceedsAttempted
    assert issue.path == ("procedure", "completed_repetitions")
    assert issue.message == "6 completed repetitions exceed 5 attempted"


def test_every_broken_rule_is_reported_in_order(adhoc):
    adhoc["coordinates"]["subject"]["components"][0]["source"] = (
        "https://example.org/other"
    )
    adhoc["procedure"]["completed_repetitions"] = 6
    assert [type(issue) for issue in check(adhoc)] == [
        PrimarySourceMismatch,
        CompletedExceedsAttempted,
    ]
