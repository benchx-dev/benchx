"""Read, schema-check, and rule-check benchx documents.

Stages return problems with the document rather than raising them, report every
one they find, and may assume earlier stages found none. A caller asked to act
on a document, not to judge it, raises `Rejected` once, with every problem.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import catalog, loader, rules, validation
from .errors import DocumentError, Refusal, Rejected, SchemaNotFound
from .hashing import canonical, order_ref, pretty, sha256_hex
from .validation import Kind

__all__ = [
    "Inspection",
    "Refusal",
    "Rejected",
    "canonical",
    "pretty",
    "check",
    "check_precision",
    "inspect",
    "order_ref",
    "sha256_hex",
]


@dataclass
class Inspection:
    path: Path
    kind: Kind
    data: dict[str, Any] | None  # None unless valid
    errors: Sequence[DocumentError]

    @property
    def valid(self) -> bool:
        return not self.errors


def inspect(path: Path, kind: Kind) -> Inspection:
    data, read_errors = loader.read(path)
    if data is None:
        return Inspection(path, kind, None, read_errors)

    errors = check(data, kind)
    return Inspection(path, kind, None if errors else data, errors)


def check(data: dict[str, Any], kind: Kind) -> Sequence[DocumentError]:
    validator, schema_errors = validation.find_schema(data, kind)
    if validator is None:
        return schema_errors

    errors: list[DocumentError] = list(validation.check(data, validator))
    if not errors:
        errors = list(rules.check(data, kind))
    return errors


def check_precision(precision: dict[str, Any]) -> Sequence[DocumentError]:
    """A work order's precision block on its own, as a config file holds it."""
    release = catalog.release("work-order", catalog.WORK_ORDER)
    validator = catalog.fragment("work-order", release, "/properties/precision")
    if validator is None:
        return [SchemaNotFound(reason=f"no schema for work-order release {release}")]
    return list(validation.check(precision, validator))
