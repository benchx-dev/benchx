"""The registry of comparison methods (comparator.md §4). Adding one is its own
module and a line here."""

from ..evidence import CompareError, Method
from . import paired_relative

METHODS = {method.id: method for method in (paired_relative.METHOD,)}
DEFAULT = paired_relative.METHOD.id


def get(name: str) -> Method:
    if name not in METHODS:
        raise CompareError(f"method {name!r} is not in the implementation ({', '.join(METHODS)})")
    return METHODS[name]
