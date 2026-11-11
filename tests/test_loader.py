import pytest

from benchx.core.errors import BadNumber, DuplicateKey, ReadError
from benchx.core.loader import read


def errors(path) -> list[ReadError]:
    data, found = read(path)
    assert data is None and found
    return found


def error(path) -> ReadError:
    [only] = errors(path)
    return only


def test_reads_object(write):
    assert read(write('{"a": [1, 2.5, "x", null, true]}')) == (
        {"a": [1, 2.5, "x", None, True]},
        [],
    )


def test_missing_file(tmp_path):
    issue = error(tmp_path / "absent.json")
    assert type(issue) is ReadError and "cannot read file" in issue.message


def test_invalid_utf8(write):
    issue = error(write(b'{"a": "\xff"}'))
    assert type(issue) is ReadError and issue.message == "invalid UTF-8 at byte 7"


def test_syntax_error_has_line_and_column(write):
    issue = error(write('{\n  "a": 1,\n  oops\n}'))
    assert (issue.line, issue.column) == (3, 3)
    assert str(issue).startswith("malformed at line 3, column 3:")


def test_byte_order_mark_is_rejected(write):
    assert type(error(write("﻿{}"))) is ReadError


@pytest.mark.parametrize("text", ["[]", "1", '"x"', "null"])
def test_top_level_must_be_object(write, text):
    issue = error(write(text))
    assert (
        type(issue) is ReadError and issue.message == "top level must be a JSON object"
    )


def test_unpaired_surrogate(write):
    issue = error(write('{"a": "\\ud800"}'))
    assert type(issue) is ReadError and "unpaired surrogate" in issue.message


def test_duplicate_key(write):
    issue = error(write('{"a": 1, "b": {"c": 1, "c": 2}}'))
    assert issue == DuplicateKey(key="c")
    assert str(issue) == "malformed: duplicate key 'c'"


@pytest.mark.parametrize(
    "literal, reason",
    [
        ("NaN", "not a JSON number"),
        ("Infinity", "not a JSON number"),
        ("-Infinity", "not a JSON number"),
        ("1e400", "overflows a double"),
        ("9007199254740992", "cannot be represented exactly"),
        ("-9007199254740992", "cannot be represented exactly"),
    ],
)
def test_bad_numbers(write, literal, reason):
    issue = error(write(f'{{"x": [0, {literal}]}}'))
    assert type(issue) is BadNumber and issue.literal == literal
    assert reason in issue.message


def test_largest_exact_integer_is_fine(write):
    data, _ = read(write('{"x": 9007199254740991, "y": -9007199254740991}'))
    assert data == {"x": 2**53 - 1, "y": -(2**53 - 1)}


def test_integer_beyond_conversion_limit(write):
    issue = error(write('{"x": ' + "9" * 5000 + "}"))
    assert type(issue) is BadNumber and "too large" in issue.message


def test_reports_every_problem(write):
    # Duplicate keys surface when their object closes, after its values.
    found = errors(write('{"a": NaN, "a": 1, "b": [Infinity]}'))
    assert found == [
        BadNumber(literal="NaN", reason="not a JSON number"),
        BadNumber(literal="Infinity", reason="not a JSON number"),
        DuplicateKey(key="a"),
    ]


def test_syntax_error_follows_problems_found_before_it(write):
    found = errors(write('{"a": NaN, oops'))
    assert [type(issue) for issue in found] == [BadNumber, ReadError]
