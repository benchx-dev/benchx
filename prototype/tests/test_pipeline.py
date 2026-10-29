import json

import pytest

from benchx.core import inspect
from benchx.core.errors import (
    BadNumber,
    Code,
    DocumentError,
    PrimarySourceMismatch,
    ReadError,
    SchemaNotFound,
    StructureError,
)


def errors(path) -> list[DocumentError]:
    inspection = inspect(path, "measurement-result")
    assert not inspection.valid and inspection.data is None
    return inspection.errors


def types(path) -> list[type]:
    return [type(error) for error in errors(path)]


def test_valid_document_is_kept(write, adhoc):
    inspection = inspect(write(adhoc), "measurement-result")
    assert inspection.valid and inspection.errors == []
    assert inspection.data == adhoc


def test_unknown_kind_is_a_programming_error(write, adhoc):
    with pytest.raises(AssertionError, match="'nonsense'"):
        inspect(write(adhoc), "nonsense")


def test_read_errors(write):
    assert types(write('{"schema_version": "benchx/measurement-result/0.1.0", "x": NaN, "y": 1e400}')) == [
        BadNumber,
        BadNumber,
    ]


def test_unparseable_file(write):
    assert types(write("{")) == [ReadError]


def test_missing_schema(write):
    assert types(write({"schema_version": "benchx/measurement-result/99.0.0", "anything": "goes"})) == [SchemaNotFound]


def test_structure_checked_before_rules(write, adhoc):
    # Breaks a rule too, and would crash rules that assume structure if they ran.
    adhoc["coordinates"]["subject"]["components"][0]["source"] = "https://x.org/y"
    del adhoc["coordinates"]["subject"]
    [issue] = errors(write(adhoc))
    assert type(issue) is StructureError and issue.path == ("coordinates",)


def test_rules_run_when_structure_passes(write, adhoc):
    adhoc["coordinates"]["subject"]["components"][0]["source"] = (
        "https://example.org/other"
    )
    assert types(write(adhoc)) == [PrimarySourceMismatch]


def test_error_rendering(write, adhoc):
    adhoc["procedure"]["slot"] = -1
    [issue] = errors(write(adhoc))
    assert (
        str(issue) == "malformed at /procedure/slot: -1 is less than the minimum of 0"
    )


def test_codes_serialize_as_strings():
    assert json.dumps({"code": Code.UNIT_CONFLICT}) == '{"code": "unit-conflict"}'
