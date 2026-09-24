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

import json_to_parquet  # noqa: E402
from json_to_parquet import arrow_type, load_json, main  # noqa: E402

SCHEMA_PATH = ROOT / "schemas/measurement-result/0.1.0/schema.json"
EXAMPLES = sorted((ROOT / "schemas/measurement-result/0.1.0/examples").glob("*.json"))


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.tmp.name) / "results.parquet"
        main(SCHEMA_PATH, cls.output, *EXAMPLES)
        cls.table = pq.read_table(cls.output)
        cls.messages = [load_json(path) for path in EXAMPLES]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_one_row_per_message(self):
        self.assertEqual(self.table.num_rows, len(EXAMPLES))
        self.assertEqual(self.table.column("ingest_key").to_pylist(), [m["ingest_key"] for m in self.messages])

    def test_required_fields_are_not_nullable(self):
        schema = self.table.schema
        self.assertFalse(schema.field("ingest_key").nullable)
        self.assertTrue(schema.field("project").nullable)
        observation = schema.field("measurement").type.field("observations").type.value_type
        self.assertFalse(observation.field("value").nullable)
        self.assertTrue(observation.field("inner_iterations").nullable)

    def test_open_objects_are_canonical_json(self):
        self.assertEqual(self.table.schema.field("procedure").type, pa.json_())
        for message, stored in zip(self.messages, self.table.column("procedure").to_pylist()):
            expected = rfc8785.dumps(message["procedure"]).decode() if "procedure" in message else None
            self.assertEqual(stored, expected)

    def test_observations_keep_their_values(self):
        for message, stored in zip(self.messages, self.table.column("measurement").to_pylist()):
            expected = message["measurement"].get("observations")
            if expected is None:
                self.assertIsNone(stored["observations"])
                continue
            values = [o["value"] if isinstance(o, dict) else o for o in expected]
            self.assertEqual([o["value"] for o in stored["observations"]], values)

    def test_file_metadata_names_the_message_schema(self):
        metadata = self.table.schema.metadata
        self.assertEqual(metadata[b"benchx.schema_uri"], b"urn:benchx:schema:measurement-result:0.1.0")
        self.assertEqual(len(metadata[b"benchx.message_schema_sha256"]), 64)


class StrictInputTests(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(text)
        try:
            return load_json(f.name)
        finally:
            Path(f.name).unlink()

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(ValueError):
            self.load('{"a": 1, "a": 2}')

    def test_rejects_non_finite_numbers(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.assertRaises(ValueError):
                self.load(f'{{"a": {token}}}')

    def test_rejects_unsafe_integers(self):
        with self.assertRaises(Exception):
            self.load(json.dumps({"a": 2**53 + 1}))


class TypeMappingTests(unittest.TestCase):
    def map(self, node, defs=None):
        return arrow_type(node, defs or {}, pa.json_())

    def test_literals(self):
        self.assertEqual(self.map({"const": 5}), pa.int64())
        self.assertEqual(self.map({"const": True}), pa.bool_())
        self.assertEqual(self.map({"enum": [1, 2]}), pa.int64())
        self.assertEqual(self.map({"enum": [1, 2.5]}), pa.float64())
        self.assertEqual(self.map({"enum": ["a", "b"]}), pa.string())
        self.assertEqual(self.map({"enum": ["a", 1]}), pa.json_())

    def test_union_of_primitives_stays_json(self):
        self.assertEqual(self.map({"oneOf": [{"type": "string"}, {"type": "integer"}]}), pa.json_())

    def test_conflicting_union_fails(self):
        node = {"oneOf": [
            {"type": "object", "additionalProperties": False, "properties": {"x": {"type": "string"}}},
            {"type": "object", "additionalProperties": False, "properties": {"x": {"type": "integer"}}},
        ]}
        with self.assertRaises(ValueError):
            self.map(node)

    def test_every_def_maps(self):
        schema = json_to_parquet.load_json(SCHEMA_PATH)
        for name, node in schema["$defs"].items():
            with self.subTest(name=name):
                self.map(node, schema["$defs"])


if __name__ == "__main__":
    unittest.main()
