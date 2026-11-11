"""Read a file into a JSON object, rejecting what `json` accepts but isn't portable."""

import json
import math
from pathlib import Path
from typing import Any

from .errors import BadNumber, DuplicateKey, ReadError

# Largest integer an IEEE double, and so RFC 8785, represents exactly.
MAX_EXACT_INTEGER = 2**53 - 1


def read(path: Path) -> tuple[dict[str, Any] | None, list[ReadError]]:
    text = read_text(path)
    if isinstance(text, ReadError):
        return None, [text]
    return parse(text)


def parse(text: str) -> tuple[dict[str, Any] | None, list[ReadError]]:
    errors: list[ReadError] = []
    data = _parse(text, errors)
    if errors:
        return None, errors

    if not isinstance(data, dict):
        return None, [ReadError(reason="top level must be a JSON object")]

    try:
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None, [ReadError(reason="string contains an unpaired surrogate")]

    return data, []


def read_text(path: Path) -> str | ReadError:
    """The file's text, or why it cannot be read: not there, not readable, not UTF-8."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ReadError(reason=f"cannot read file: {exc.strerror or exc}")

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ReadError(reason=f"invalid UTF-8 at byte {exc.start}")


def _parse(text: str, errors: list[ReadError]) -> Any:
    # The hooks record problems and return stand-ins, so parsing carries on.

    def parse_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, value in pairs:
            if key in obj:
                errors.append(DuplicateKey(key=key))
            obj[key] = value
        return obj

    def parse_constant(literal: str) -> float:
        errors.append(BadNumber(literal=literal, reason="not a JSON number"))
        return math.nan

    def parse_float(literal: str) -> float:
        value = float(literal)
        if not math.isfinite(value):
            errors.append(BadNumber(literal=literal, reason="overflows a double"))
        return value

    def parse_int(literal: str) -> int:
        try:
            value = int(literal)
        except ValueError:
            # Beyond Python's integer string conversion limit (4300 digits).
            errors.append(
                BadNumber(literal=literal[:20] + "…", reason="integer too large")
            )
            return 0

        if abs(value) > MAX_EXACT_INTEGER:
            reason = "integer outside ±(2**53 - 1) cannot be represented exactly"
            errors.append(BadNumber(literal=literal, reason=reason))
        return value

    try:
        return json.loads(
            text,
            object_pairs_hook=parse_object,
            parse_constant=parse_constant,
            parse_float=parse_float,
            parse_int=parse_int,
        )
    except json.JSONDecodeError as exc:
        errors.append(ReadError(reason=exc.msg, line=exc.lineno, column=exc.colno))
    except RecursionError:
        errors.append(ReadError(reason="nesting too deep"))
    return None
