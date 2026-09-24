"""Tests for tools/json_to_parquet.py.

    pip install jsonschema 'pyarrow>=19' rfc8785
    python -m unittest tests/test_json_to_parquet.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import rfc8785

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from json_to_parquet import arrow_type, load_json, main  # noqa: E402

SCHEMA_PATH = ROOT / "schemas/measurement-result/0.1.0/schema.json"
EXAMPLES = sorted((ROOT / "schemas/measurement-result/0.1.0/examples").glob("*.json"))


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "results.parquet"
            main(SCHEMA_PATH, output, *EXAMPLES)
            cls.table = pq.read_table(output)
        cls.messages = [load_json(path) for path in EXAMPLES]

    def test_rows_and_nullability(self):
        self.assertEqual(self.table.column("ingest_key").to_pylist(), [m["ingest_key"] for m in self.messages])
        self.assertFalse(self.table.schema.field("ingest_key").nullable)
        self.assertTrue(self.table.schema.field("project").nullable)

    def test_open_objects_are_canonical_json(self):
        self.assertEqual(self.table.schema.field("procedure").type, pa.json_())
        for message, stored in zip(self.messages, self.table.column("procedure").to_pylist()):
            expected = rfc8785.dumps(message["procedure"]).decode() if "procedure" in message else None
            self.assertEqual(stored, expected)

    def test_observations_keep_their_values(self):
        for message, stored in zip(self.messages, self.table.column("measurement").to_pylist()):
            expected = message["measurement"].get("observations")
            values = None if expected is None else [o["value"] if isinstance(o, dict) else o for o in expected]
            self.assertEqual(values, None if stored["observations"] is None else [o["value"] for o in stored["observations"]])

    def test_file_metadata_names_the_message_schema(self):
        self.assertEqual(self.table.schema.metadata[b"benchx.schema_uri"], b"urn:benchx:schema:measurement-result:0.1.0")


class StrictInputTests(unittest.TestCase):
    # The message schema accepts NaN and integers beyond 2**53 once parsed, and
    # never sees duplicate keys, so these checks are the loader's alone.
    def test_rejects_what_the_schema_cannot_see(self):
        for text in ('{"a": 1, "a": 2}', '{"a": NaN}', '{"a": Infinity}', json.dumps({"a": 2**53 + 1})):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "m.json"
                path.write_text(text)
                with self.assertRaises(Exception):
                    load_json(path)


class TypeMappingTests(unittest.TestCase):
    def test_unsupported_nodes_fail(self):
        closed = lambda t: {"type": "object", "additionalProperties": False, "properties": {"x": {"type": t}}}  # noqa: E731
        for node in (
            {"const": True},
            {"enum": ["a", 1]},
            {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            {"oneOf": [closed("string"), closed("integer")]},
        ):
            with self.subTest(node=node), self.assertRaises(ValueError):
                arrow_type(node, {}, pa.json_())


if __name__ == "__main__":
    unittest.main()
