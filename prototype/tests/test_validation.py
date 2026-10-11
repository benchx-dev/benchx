import json

import pytest
from jsonschema import Draft202012Validator

from benchx.core import catalog, validation
from benchx.core.errors import Code, SchemaNotFound, StructureError

TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def check(data: dict) -> list[StructureError]:
    validator, errors = validation.find_schema(data, "measurement-result")
    assert not errors
    return list(validation.check(data, validator))


SCHEMA_FILES = sorted(
    (kind.name, version.name, version / "schema.json")
    for kind in catalog.SCHEMAS.iterdir()
    for version in kind.iterdir()
)


@pytest.mark.parametrize("kind, version, file", SCHEMA_FILES, ids=lambda v: str(v)[:40])
def test_schemas_are_valid_json_schema(kind, version, file):
    Draft202012Validator.check_schema(json.loads(file.read_text("utf-8")))


def test_result_schema_is_present():
    assert ("measurement-result", "0.1.0") in {
        (kind, version) for kind, version, _ in SCHEMA_FILES
    }


def test_lookup_is_cached():
    assert catalog.lookup("measurement-result", 5) is catalog.lookup(
        "measurement-result", 5
    )


def test_lookup_of_missing_schema_is_none():
    assert catalog.lookup("measurement-result", 4) is None


def test_finds_result_schema(adhoc):
    validator, errors = validation.find_schema(adhoc, "measurement-result")
    assert errors == []
    assert validator.schema["$id"] == "urn:benchx:schema:measurement-result:0.1.0"


@pytest.mark.parametrize(
    "data, kind, path, fragment",
    [
        ({"producer": {}}, "measurement-result", (), "no schema_version field"),
        (
            {"comparison_version": 1},
            "measurement-result",
            (),
            "expected a measurement-result",
        ),
        (
            {"schema_version": "5"},
            "measurement-result",
            ("schema_version",),
            "no schema for measurement-result version '5'",
        ),
        (
            {"schema_version": True},
            "measurement-result",
            ("schema_version",),
            "must be an integer or a string",
        ),
        (
            {"schema_version": 4},
            "measurement-result",
            ("schema_version",),
            "no schema for measurement-result version 4",
        ),
        (
            {"schema_version": "benchx/work-order/9.0.0"},
            "work-order",
            ("schema_version",),
            "no schema for work-order version 'benchx/work-order/9.0.0'",
        ),
        (
            {"schema_version": "benchx/work-order/../../measurement-result/0.1.0"},
            "work-order",
            ("schema_version",),
            "no schema for work-order",
        ),
    ],
)
def test_schema_not_found(data, kind, path, fragment):
    validator, [issue] = validation.find_schema(data, kind)
    assert validator is None and type(issue) is SchemaNotFound
    assert issue.path == path and fragment in issue.message


def test_examples_fit(example):
    assert check(example) == []


def test_censored_skipped_and_workorder_artifact_fit(adhoc):
    censored = adhoc | {
        "measurement": {
            "status": "censored",
            "reason": "timeout",
            "constraint": {
                "kind": "lower_bound",
                "lower": 60,
                "lower_inclusive": False,
                "cause": "timeout",
            },
        }
    }
    skipped = adhoc | {"measurement": {"status": "skipped", "reason": "not-applicable"}}
    linked = json.loads(json.dumps(adhoc))
    linked["provenance"]["artifacts"] = [
        {
            "kind": "workorder",
            "media_type": "application/json",
            "uri": "file:///tmp/wo.json",
            "sha256": "a" * 64,
        }
    ]
    assert check(censored) == check(skipped) == check(linked) == []


def test_unexpected_property_is_malformed(adhoc):
    adhoc["series_fingerprint"] = "abc"
    [issue] = check(adhoc)
    assert type(issue) is StructureError and issue.code is Code.MALFORMED
    assert (issue.path, issue.keyword) == ((), "additionalProperties")
    assert (
        issue.message
        == "Additional properties are not allowed ('series_fingerprint' was unexpected)"
    )


@pytest.mark.parametrize(
    "mutate, path, keyword, message",
    [
        (
            lambda d: d.pop("ingest_key"),
            (),
            "required",
            "'ingest_key' is a required property",
        ),
        (
            lambda d: d["provenance"].pop("subject_tree"),
            ("provenance",),
            "required",
            "'subject_tree' is a required property",
        ),
        (
            lambda d: d["provenance"].__setitem__("benchmark_tree", TREE),
            ("provenance",),
            "not",
            None,
        ),
        (
            lambda d: d["measurement"].__setitem__("observations", [{"value": "slow"}]),
            ("measurement", "observations", 0),
            "oneOf",
            None,
        ),
        (
            lambda d: d["measurement"].update({"status": "error", "reason": "timeout"}),
            ("measurement",),
            "not",
            None,
        ),
        (
            lambda d: d["measurement"].__setitem__("status", "partial"),
            ("measurement",),
            "required",
            "'reason' is a required property",
        ),
        (
            lambda d: d["coordinates"]["environment"].pop("identity"),
            ("coordinates", "environment"),
            "required",
            "'identity' is a required property",
        ),
        (
            lambda d: d["coordinates"]["subject"]["components"].append(
                {"role": "primary"}
            ),
            ("coordinates", "subject", "components"),
            "maxContains",
            "Too many items match the given schema (expected at most 1)",
        ),
        (
            lambda d: d["source"].__setitem__("uri", "not a uri"),
            ("source", "uri"),
            "format",
            "'not a uri' is not a 'uri'",
        ),
        (
            lambda d: d["provenance"].__setitem__(
                "started_at", "2026-09-14T10:02:31+02:00"
            ),
            ("provenance", "started_at"),
            "pattern",
            None,
        ),
        (
            lambda d: d["procedure"].__setitem__("round", -1),
            ("procedure", "round"),
            "minimum",
            "-1 is less than the minimum of 0",
        ),
    ],
)
def test_structure_errors(adhoc, mutate, path, keyword, message):
    mutate(adhoc)
    [issue] = check(adhoc)
    assert type(issue) is StructureError
    assert (issue.path, issue.keyword) == (path, keyword)
    if message is not None:
        assert issue.message == message


def test_reports_every_structure_error(adhoc):
    adhoc["measurement"]["status"] = "done"
    adhoc["procedure"]["round"] = -1
    issues = check(adhoc)
    assert {(issue.path, issue.keyword) for issue in issues} == {
        (("measurement", "status"), "enum"),
        (("procedure", "round"), "minimum"),
    }
    assert issues == check(json.loads(json.dumps(adhoc)))
