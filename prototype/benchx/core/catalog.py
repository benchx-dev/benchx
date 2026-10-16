"""The schemas in the repository's schemas/ directory, or in $BENCHX_SCHEMAS."""

import json
import os
import re
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMAS = Path(
    os.environ.get("BENCHX_SCHEMAS") or Path(__file__).resolve().parents[3] / "schemas"
)

RELEASE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def release(kind: str, version: str) -> str | None:
    """The release a declared version names, such as "0.1.0" for
    "benchx/work-order/0.1.0"."""
    prefix = f"benchx/{kind}/"
    named = version.removeprefix(prefix)
    # The release becomes part of a file path, so it must be exactly this shape.
    if version.startswith(prefix) and RELEASE.fullmatch(named):
        return named
    return None


@cache
def lookup(kind: str, version: str) -> Draft202012Validator | None:
    named = release(kind, version)
    schema = _read(kind, named) if named else None
    if schema is None:
        return None

    return Draft202012Validator(
        schema, format_checker=FormatChecker(), registry=_registry()
    )


@cache
def fragment(kind: str, release: str, pointer: str) -> Draft202012Validator | None:
    """A validator for one part of a schema, such as a work order's precision."""
    schema = _read(kind, release)
    if schema is None:
        return None

    return Draft202012Validator(
        {"$ref": f"{schema['$id']}#{pointer}"},
        format_checker=FormatChecker(),
        registry=_registry(),
    )


@cache
def _read(kind: str, release: str) -> dict | None:
    file = SCHEMAS / kind / release / "schema.json"
    if not file.is_file():
        return None

    schema = json.loads(file.read_text("utf-8"))
    if kind == "work-order":
        # Prototype extension: assigned by whoever coordinates the sides
        # (runner-schema.md §3), not by the runner.
        schema["properties"]["slot"] = {
            "description": "The realized position across all sides; copied to procedure.slot.",
            "type": "integer",
            "minimum": 0,
        }
    return schema


@cache
def _registry() -> Registry:
    # Schemas refer to each other by $id, so each validator sees all of them.
    schemas = [
        _read(kind.name, release.name)
        for kind in sorted(SCHEMAS.iterdir())
        if kind.is_dir()
        for release in sorted(kind.iterdir())
        if release.is_dir()
    ]
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas
        if schema is not None
    )
