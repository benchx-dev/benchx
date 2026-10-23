#!/usr/bin/env python3
"""Store measurement-result JSON messages in a Parquet file with PyArrow.

The Arrow schema is derived from the message JSON Schema, so it cannot drift:
closed objects become structs, open objects canonical JSON strings, and an
observation, bare number or structured, one struct. See
docs/design/measurement-result-parquet.md.

    pip install jsonschema 'pyarrow>=19' rfc8785   # Python 3.11 or newer
    python tools/json_to_parquet.py schemas/measurement-result/0.1.0/schema.json \
        results.parquet schemas/measurement-result/0.1.0/examples/*.json
"""

import hashlib
import json
import sys
from datetime import datetime

import jsonschema
import pyarrow as pa
import pyarrow.parquet as pq
import rfc8785

TIMESTAMP = pa.timestamp("us", tz="UTC")
SCALARS = {"string": pa.string(), "integer": pa.int64(), "number": pa.float64(), "boolean": pa.bool_()}


def arrow_type(node, defs, json_type):
    """Map one JSON Schema node to an Arrow type."""
    if "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    nested = lambda n: arrow_type(n, defs, json_type)  # noqa: E731
    if "allOf" in node and "type" not in node:  # a reference narrowed by assertions
        return nested(node["allOf"][0])
    if "oneOf" in node:  # union of closed objects, or number-or-object observation
        branches = [defs[b["$ref"].rsplit("/", 1)[-1]] if "$ref" in b else b for b in node["oneOf"]]
        objects = [b for b in branches if b.get("type") == "object"]
        if not objects:
            raise ValueError(f"unsupported oneOf without an object alternative: {node}")
        required = set.intersection(*(set(b.get("required", [])) for b in objects))
        types = {}  # in first-appearance order
        for b in objects:
            for k, v in b["properties"].items():
                t = nested(v)
                if types.setdefault(k, t) != t:
                    raise ValueError(f"oneOf alternatives disagree on the type of {k!r}: {types[k]} and {t}")
        return pa.struct([pa.field(k, t, k not in required) for k, t in types.items()])
    if "const" in node or "enum" in node:
        literals = {type(v) for v in node.get("enum", [node.get("const")])}
        if literals not in ({str}, {int}):  # bool is not int here: type(True) is bool
            raise ValueError(f"unsupported const or enum: {node}")
        return pa.string() if literals == {str} else pa.int64()
    if node.get("type") == "array":
        return pa.list_(pa.field("element", nested(node["items"]), False))
    if node.get("type") == "object":
        if node.get("additionalProperties") is not False or "properties" not in node:
            return json_type
        required = node.get("required", [])
        return pa.struct([pa.field(k, nested(v), k not in required) for k, v in node["properties"].items()])
    if node.get("format") == "date-time":
        return TIMESTAMP
    scalar = node.get("type")
    return SCALARS[scalar] if isinstance(scalar, str) else json_type  # mixed primitives stay JSON


def to_row(data_type, value):
    """Shape one message value for its Arrow type."""
    if value is None:
        return None
    if pa.types.is_struct(data_type):
        if not isinstance(value, dict):  # a bare observation number
            value = {"value": value}
        return {f.name: to_row(f.type, value.get(f.name)) for f in data_type}
    if pa.types.is_list(data_type):
        return [to_row(data_type.value_type, v) for v in value]
    if data_type == pa.json_():  # open object as canonical JSON
        return rfc8785.dumps(value).decode()
    if data_type == TIMESTAMP:
        return datetime.fromisoformat(value)
    return value


def unique_keys(pairs):
    # jsonschema only sees the parsed mapping, after duplicate keys are lost.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError(f"duplicate JSON key in {[key for key, _ in pairs]}")
    return result


def non_finite(token):
    # Python's JSON parser accepts NaN and Infinity even though JSON does not.
    raise ValueError(f"invalid JSON number: {token}")


def load_json(path):
    """Load strict I-JSON: UTF-8, unique keys, finite numbers, safe integers."""
    with open(path, encoding="utf-8") as f:
        value = json.load(f, object_pairs_hook=unique_keys, parse_constant=non_finite)
    rfc8785.dumps(value)  # rejects numbers outside the RFC 8785 domain, such as integers beyond 2**53
    return value


def main(schema_path, output, *message_paths):
    message_schema = load_json(schema_path)
    messages = [load_json(path) for path in message_paths]
    for message in messages:
        jsonschema.validate(message, message_schema)

    defs = message_schema["$defs"]
    # Build with strings, then cast: PyArrow cannot fill nested JSON fields
    # from Python values directly.
    build = arrow_type(message_schema, defs, pa.string())
    final = arrow_type(message_schema, defs, pa.json_())
    table = pa.Table.from_pylist([to_row(pa.struct(final), m) for m in messages], schema=pa.schema(build))
    table = table.cast(pa.schema(final)).replace_schema_metadata({
        "benchx.schema_uri": message_schema["$id"],
        "benchx.message_schema_sha256": hashlib.sha256(rfc8785.dumps(message_schema)).hexdigest(),
    })
    pq.write_table(table, output, compression="zstd")


if __name__ == "__main__":
    main(*sys.argv[1:])
