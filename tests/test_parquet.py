"""The message columns of a result (measurement-result-parquet.md) and tools/json_to_parquet.py."""

import hashlib
import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
import rfc8785

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq

from benchx import parquet
from benchx.core import loader
from benchx.core.catalog import SCHEMAS

EXAMPLES = sorted((SCHEMAS / "measurement-result" / "0.1.0" / "examples").glob("*.json"))
# The tool lives in the checkout, not the package, so an installed copy skips its tests.
TOOL = Path(__file__).resolve().parents[1] / "tools" / "json_to_parquet.py"
DOCUMENT = Path(__file__).resolve().parents[1] / "docs" / "design" / "measurement-result-parquet.md"


@pytest.fixture(scope="module")
def messages():
    return [loader.read(path)[0] for path in EXAMPLES]


@pytest.fixture(scope="module")
def table(messages):
    return parquet.message_table(messages)


def test_rows_and_nullability(table, messages):
    assert table.schema == parquet.MESSAGE_SCHEMA
    assert table.column("ingest_key").to_pylist() == [m["ingest_key"] for m in messages]
    assert not table.schema.field("ingest_key").nullable
    assert table.schema.field("project").nullable


def test_open_objects_are_canonical_json(table, messages):
    assert table.schema.field("procedure").type == pa.json_()
    expected = [rfc8785.dumps(m["procedure"]).decode() if "procedure" in m else None for m in messages]
    assert table.column("procedure").to_pylist() == expected


def test_observations_keep_their_values(table, messages):
    for message, stored in zip(messages, table.column("measurement").to_pylist()):
        reported = message["measurement"].get("observations")
        values = None if reported is None else [o["value"] if isinstance(o, dict) else o for o in reported]
        assert values == (None if stored["observations"] is None else [o["value"] for o in stored["observations"]])


def _reported(data_type, value):
    """A message value as the columns keep it (measurement-result-parquet.md §6)."""
    if pa.types.is_struct(data_type):
        if not isinstance(value, dict):  # a bare observation number
            value = {"value": value}
        # A key the struct lacks is left as it is, so the comparison shows it as dropped.
        return {
            k: _reported(data_type.field(k).type, v) if data_type.get_field_index(k) >= 0 else v
            for k, v in value.items()
        }
    if pa.types.is_list(data_type):
        return [_reported(data_type.value_type, v) for v in value]
    return datetime.fromisoformat(value) if data_type == parquet.TIMESTAMP else value


def _stored(data_type, value):
    """A stored value as the message reported it: nulls are absent members, JSON text is parsed."""
    if value is None:
        return None
    if pa.types.is_struct(data_type):
        return {f.name: v for f in data_type if (v := _stored(f.type, value[f.name])) is not None}
    if pa.types.is_list(data_type):
        return [_stored(data_type.value_type, v) for v in value]
    return json.loads(value) if data_type == pa.json_() else value


def test_nothing_reported_is_dropped(table, messages):
    row_type = pa.struct(list(table.schema))
    for message, row in zip(messages, table.to_pylist()):
        assert _stored(row_type, row) == _reported(row_type, message)


def _type_name(data_type):
    if pa.types.is_list(data_type):
        return f"list of {_type_name(data_type.value_type)}"
    if pa.types.is_struct(data_type):
        return "struct"
    if data_type == pa.json_():
        return "json"
    if data_type == parquet.TIMESTAMP:
        return "timestamp[us, UTC]"
    return "float64" if data_type == pa.float64() else str(data_type)


def _schema_fields(struct, prefix=""):
    """Every field of an Arrow struct or schema, nested ones included, as (path, field)."""
    for field in struct:
        yield prefix + field.name, field
        inner = field.type.value_type if pa.types.is_list(field.type) else field.type
        if pa.types.is_struct(inner):
            yield from _schema_fields(inner, f"{prefix}{field.name}.")


def _listed_fields(listing):
    """The §3 listing as {path: type, `*` when non-nullable}, and the structs it
    abbreviates as `(as <field>)` as {path: field}. Indentation nests."""
    fields, same, parents = {}, {}, []  # parents: (indent, path) of the structs still open
    for line in listing.splitlines():
        indent = len(line) - len(line.lstrip())
        while parents and parents[-1][0] >= indent:
            parents.pop()
        entries, alias = re.fullmatch(r"(.*?),?\s*(?:\(as (\w+)\))?", line.strip()).groups()
        for entry in re.split(r", (?=\w+\*?: )", entries):
            name, data_type = entry.split(": ")
            path = ".".join([p for _, p in parents[-1:]] + [name.rstrip("*")])
            fields[path] = data_type + "*" * name.endswith("*")
        if alias:
            same[path] = alias
        elif data_type.endswith("struct"):
            parents.append((indent, path))
    return fields, same


def test_the_documented_listing_is_the_derived_schema():
    if not DOCUMENT.exists():
        pytest.skip("docs/ is not part of an installed benchx")
    listed, same = _listed_fields(DOCUMENT.read_text().split("```text\n")[1].split("```")[0])
    derived = dict(_schema_fields(parquet.MESSAGE_SCHEMA))
    structs = {}  # the first struct of each name, which is what `(as <field>)` points at
    for path, field in derived.items():
        if pa.types.is_struct(field.type):
            structs.setdefault(path.rsplit(".", 1)[-1], field.type)
    for path, name in same.items():
        assert derived[path].type == structs[name], f"{path} is not as {name}"
    assert listed == {
        path: _type_name(field.type) + "*" * (not field.nullable)
        for path, field in derived.items()
        if not any(path.startswith(f"{abbreviated}.") for abbreviated in same)
    }


def test_metadata_names_the_message_schema(table):
    metadata = table.schema.metadata
    assert metadata[b"benchx.schema_uri"] == parquet.RESULT_SCHEMA["$id"].encode()
    digest = hashlib.sha256(rfc8785.dumps(parquet.RESULT_SCHEMA)).hexdigest()
    assert metadata[b"benchx.message_schema_sha256"] == digest.encode()


def test_store_row_begins_with_the_message_columns():
    message = list(parquet.MESSAGE_SCHEMA)
    assert list(parquet.RESULTS_SCHEMA)[: len(message)] == message
    assert parquet.RESULTS_SCHEMA.metadata == parquet.MESSAGE_SCHEMA.metadata


def closed(json_type):
    return {"type": "object", "additionalProperties": False, "properties": {"x": {"type": json_type}}}


@pytest.mark.parametrize(
    "node",
    [
        {"const": True},
        {"enum": ["a", 1]},
        {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        {"oneOf": [closed("string"), closed("integer")]},
        {"oneOf": [closed("string"), {"type": "object", "properties": {"y": {"type": "string"}}}]},  # open
        {"oneOf": [closed("string"), {"type": "object", "additionalProperties": False}]},  # no properties
        {"oneOf": [{"type": "string"}, closed("string")]},  # a bare value that is not a number
        {"oneOf": [{"type": "number"}, closed("number")]},  # no `value` to hold the bare number
        {"oneOf": [{"type": "number"}, closed("number"), closed("number")]},
        {"allOf": [{"minLength": 1}, closed("string")]},  # no leading reference
    ],
)
def test_unsupported_nodes_fail(node):
    with pytest.raises(ValueError):
        parquet.arrow_type(node, {}, pa.json_())


@pytest.fixture
def tool():
    if not TOOL.exists():
        pytest.skip("tools/ is not part of an installed benchx")
    spec = importlib.util.spec_from_file_location("json_to_parquet", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_writes_the_message_columns(tool, tmp_path, table):
    output = tmp_path / "results.parquet"
    assert tool.main(output, *EXAMPLES) == 0
    assert pq.read_table(output).equals(table, check_metadata=True)


def test_tool_writes_nothing_when_one_document_is_invalid(tool, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    text = EXAMPLES[0].read_text()
    bad.write_text(text[: text.rindex("}")] + ', "ingest_key": "again"}')  # a duplicate key
    output = tmp_path / "results.parquet"
    assert tool.main(output, EXAMPLES[0], bad) == 1
    assert not output.exists()
    assert "bad.json: malformed" in capsys.readouterr().err
