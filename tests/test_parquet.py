"""The message columns of a result (measurement-result-parquet.md) and tools/json_to_parquet.py."""

import hashlib
import importlib.util
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rfc8785

from benchx import parquet
from benchx.core import loader
from benchx.core.catalog import SCHEMAS

EXAMPLES = sorted((SCHEMAS / "measurement-result" / "0.1.0" / "examples").glob("*.json"))
# The tool lives in the checkout, not the package, so an installed copy skips its tests.
TOOL = Path(__file__).resolve().parents[1] / "tools" / "json_to_parquet.py"


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


@pytest.mark.parametrize("node", [
    {"const": True},
    {"enum": ["a", 1]},
    {"oneOf": [{"type": "string"}, {"type": "integer"}]},
    {"oneOf": [closed("string"), closed("integer")]},
])
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
