"""The schemas shipped inside the package (benchx/schemas), or in $BENCHX_SCHEMAS.

Shipped schemas use only the `date-time` and `uri` JSON Schema formats, checked
via `rfc3339-validator` and `rfc3986-validator`. Custom schemas supplied through
`$BENCHX_SCHEMAS` may use additional formats if the corresponding validator
package (or `jsonschema[format-nongpl]`) is installed; missing format checkers
raise `FormatCheckerError` rather than silently skipping format validation.
"""

import json
import os
import re
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

# Package data, so an installed copy finds them just as an editable install does.
SCHEMAS = Path(os.environ.get("BENCHX_SCHEMAS") or Path(__file__).resolve().parents[1] / "schemas")

# The version string each document kind declares in its schema_version. Bump a
# schema here and nowhere else in the code.
MEASUREMENT_RESULT = "benchx/measurement-result/0.1.0"
COMPARISON_DOCUMENT = "benchx/comparison-document/0.1.0"
WORK_ORDER = "benchx/work-order/0.1.0"
TARGET_DESCRIPTION = "benchx/target-description/0.1.0"
TARGET_PROVIDER_REQUEST = "benchx/target-provider-request/0.1.0"

RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

_SUBSCHEMA_KEYS = (
    "not",
    "if",
    "then",
    "else",
    "items",
    "additionalProperties",
    "contains",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
_SUBSCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SUBSCHEMA_MAP_KEYS = (
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
)


class FormatCheckerError(RuntimeError):
    """A schema declares a `format` whose jsonschema checker is not installed."""


def release(kind: str, version: str) -> str | None:
    """The release a declared version names, such as "0.1.0" for
    "benchx/work-order/0.1.0"."""
    prefix = f"benchx/{kind}/"
    named = version.removeprefix(prefix)
    # The release becomes part of a file path, so it must be exactly this shape.
    if version.startswith(prefix) and RELEASE.fullmatch(named):
        return named
    return None


def _required_formats(schema: dict, by_id: dict[str, dict]) -> set[str]:
    """Every `format` keyword declared by `schema` and any catalog schemas it `$ref`s."""
    formats: set[str] = set()
    visited_ids: set[str] = set()
    if isinstance(schema.get("$id"), str):
        visited_ids.add(schema["$id"])

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        fmt = node.get("format")
        if isinstance(fmt, str):
            formats.add(fmt)
        ref = node.get("$ref")
        if isinstance(ref, str):
            target_id = ref.split("#", 1)[0]
            if target_id and target_id not in visited_ids and target_id in by_id:
                visited_ids.add(target_id)
                walk(by_id[target_id])
        for key in _SUBSCHEMA_KEYS:
            walk(node.get(key))
        for key in _SUBSCHEMA_LIST_KEYS:
            items = node.get(key)
            if isinstance(items, list):
                for item in items:
                    walk(item)
        for key in _SUBSCHEMA_MAP_KEYS:
            mapping = node.get(key)
            if isinstance(mapping, dict):
                for sub in mapping.values():
                    walk(sub)

    walk(schema)
    return formats


def _format_checker(schema: dict) -> FormatChecker:
    """A FormatChecker verified to support every `format` declared by `schema`."""
    checker = FormatChecker()
    required = _required_formats(schema, _schemas_by_id())
    missing = sorted(fmt for fmt in required if fmt not in checker.checkers)
    if missing:
        names = ", ".join(missing)
        raise FormatCheckerError(
            f"missing jsonschema format checker(s) for {names}; "
            "install rfc3339-validator and rfc3986-validator for the shipped schemas, "
            "or jsonschema[format-nongpl] (or the format's validator package) for custom $BENCHX_SCHEMAS"
        )
    return checker


@cache
def lookup(kind: str, version: str) -> Draft202012Validator | None:
    named = release(kind, version)
    schema = _read(kind, named) if named else None
    if schema is None:
        return None

    return Draft202012Validator(schema, format_checker=_format_checker(schema), registry=_registry())


@cache
def fragment(kind: str, release: str, pointer: str) -> Draft202012Validator | None:
    """A validator for one part of a schema, such as a work order's precision."""
    schema = _read(kind, release)
    if schema is None:
        return None

    return Draft202012Validator(
        {"$ref": f"{schema['$id']}#{pointer}"},
        format_checker=_format_checker(schema),
        registry=_registry(),
    )


@cache
def _read(kind: str, release: str) -> dict | None:
    file = SCHEMAS / kind / release / "schema.json"
    if not file.is_file():
        return None

    schema = json.loads(file.read_text("utf-8"))
    if kind == "work-order":
        # Extension of the implementation: assigned by whoever coordinates the sides
        # (runner-schema.md §3), not by the runner.
        schema["properties"]["slot"] = {
            "description": "The realized position across all sides; copied to procedure.slot.",
            "type": "integer",
            "minimum": 0,
        }
    return schema


@cache
def _schemas_by_id() -> dict[str, dict]:
    schemas = [
        _read(kind.name, release.name)
        for kind in sorted(SCHEMAS.iterdir())
        if kind.is_dir()
        for release in sorted(kind.iterdir())
        if release.is_dir()
    ]
    return {schema["$id"]: schema for schema in schemas if schema is not None and "$id" in schema}


@cache
def _registry() -> Registry:
    # Schemas refer to each other by $id, so each validator sees all of them.
    return Registry().with_resources(
        (schema_id, Resource.from_contents(schema)) for schema_id, schema in _schemas_by_id().items()
    )


def clear_cache() -> None:
    """Reset cached schemas and validators (for tests that change SCHEMAS or FormatChecker)."""
    lookup.cache_clear()
    fragment.cache_clear()
    _read.cache_clear()
    _schemas_by_id.cache_clear()
    _registry.cache_clear()
