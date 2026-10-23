"""The store's row: the message columns #30 derives, plus the store's own.

`arrow_type` and `to_row` are the mapping of tools/json_to_parquet.py in #30
(measurement-result-parquet.md §2): closed objects become structs, open
objects canonical JSON, observations one typed list. The store adds the
columns it derives at ingest beside them (prototype-design.md §4).
"""

from datetime import datetime

import pyarrow as pa
import rfc8785

from .core import catalog

_VALIDATOR = catalog.lookup("measurement-result", 5)
if _VALIDATOR is None:
    raise RuntimeError(
        f"result schema not found under {catalog.SCHEMAS}; "
        "run from a benchx checkout or set BENCHX_SCHEMAS"
    )
RESULT_SCHEMA = _VALIDATOR.schema

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


# Columns the store derives at ingest, beside the message columns.
DERIVED = [
    pa.field("store_project", pa.string(), False),  # project, or local/<host> for a thin result
    pa.field("reported_coordinates_fingerprint", pa.string(), False),
    pa.field("payload_sha256", pa.string(), False),
    pa.field("attempt_signature", pa.string(), False),  # schema §5.3 attempt integrity
    # The document itself, canonical: #30's columns cannot reproduce it (a bare
    # number and a structured observation map alike), and the comparator reads it.
    pa.field("document", pa.json_(), False),
    pa.field("work_order_ref", pa.string()),
    pa.field("work_order", pa.json_()),  # the referenced order, when it was delivered
]

_DEFS = RESULT_SCHEMA["$defs"]
_BUILD = arrow_type(RESULT_SCHEMA, _DEFS, pa.string())
_FINAL = arrow_type(RESULT_SCHEMA, _DEFS, pa.json_())
RESULTS_SCHEMA = pa.schema(list(_FINAL) + DERIVED, metadata={
    "benchx.schema_uri": RESULT_SCHEMA["$id"],
})


def results_table(documents: list[dict], derived: list[dict]) -> pa.Table:
    """One row per result: the message as #30 maps it, then the derived columns."""
    rows = [{**to_row(_FINAL, doc), **extra} for doc, extra in zip(documents, derived)]
    # Build with strings, then cast: PyArrow cannot fill nested JSON fields
    # from Python values directly.
    build = [pa.field(f.name, pa.string(), f.nullable) if f.type == pa.json_() else f for f in DERIVED]
    table = pa.Table.from_pylist(rows, schema=pa.schema(list(_BUILD) + build))
    return table.cast(RESULTS_SCHEMA)
