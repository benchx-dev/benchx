"""Keys: how one column is read from a result (comparator.md §2.6).

A key is a JSON Pointer, an optional normalization rule, and an ordering: one
definition of how a fact is read and ordered. Projections read their axis and
roles through keys, and the comparator's evidence reads its columns through them.
"""

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .core.hashing import canonical

# -- source URIs (the canonical-uri rule) --------------------------------------

_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^@/:\s\[\]]+)@)?(?P<host>\[[0-9A-Fa-f:.]+\]|[^@/:\s\[\]]+):(?P<path>.+)$")


def normalize_remote(remote: str, base: str | None = None) -> str:
    """A git remote as a URI, whatever form it was given in; idempotent.

    A `scheme://` remote is unchanged. A Windows drive path or a local path
    (resolved against `base`, default the current directory) becomes a
    `file://` URI. git's SCP-style `[user@]host:path` becomes `ssh://`: an
    absolute path stays absolute, and a relative one (relative to the login
    directory) is kept relative as `ssh://host/~/path`, since `ssh://host/path`
    would name a different repository."""
    if not remote or "://" in remote:
        return remote
    if _WINDOWS_PATH.match(remote):
        return "file:///" + urllib.parse.quote(remote.replace("\\", "/"), safe="/:")
    match = _SCP_REMOTE.match(remote)
    if match:
        user = f"{match['user']}@" if match["user"] else ""
        path = match["path"] if match["path"].startswith("/") else "/~/" + match["path"]
        return f"ssh://{user}{match['host']}{path}"
    return Path(base or ".", remote).resolve().as_uri()


def canonical_uri(uri: str) -> str:
    parts = urllib.parse.urlsplit(normalize_remote(uri))
    path = parts.path.rstrip("/").removesuffix(".git")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _normalize(rule: dict, value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return value  # rules are defined on scalars; anything else matches as is
    if rule["rule"] == "canonical-uri":
        return canonical_uri(str(value))
    return rule["map"].get(str(value), value)  # map: unlisted values are kept


# -- pointers ------------------------------------------------------------------

def escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def get(doc: dict, pointer: str):
    """The value at a JSON Pointer, or None when any step is missing."""
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


# -- ordering ------------------------------------------------------------------

_DIGITS = re.compile(r"(\d+)")


def axis_order(value) -> tuple:
    """A sort key for one axis value: numbers by value, strings in natural
    order so that versions and sizes sort as people read them (`6.8` before
    `6.10`, `size-20` before `size-100`), then anything else by its canonical
    form, and a missing value last. Every key starts with a rank for its kind,
    so values of different kinds never compare directly."""
    if value is None:
        return (4,)
    if isinstance(value, bool):
        return (0, value)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        # re.split with a group alternates text and digits, so equal positions
        # always hold the same type; the string itself breaks ties such as `07`
        # against `7`.
        parts = _DIGITS.split(value)
        return (2, tuple(int(p) if i % 2 else p for i, p in enumerate(parts)), value)
    return (3, canonical(value).decode())


_ORDERINGS = {"natural": axis_order}


# -- keys ----------------------------------------------------------------------

@dataclass(frozen=True)
class Key:
    pointer: str
    normalize: dict | None = field(default=None, hash=False)
    order: str = "natural"

    def normalized(self, value):
        return value if self.normalize is None else _normalize(self.normalize, value)

    def read(self, doc: dict):
        """This key's value in `doc`, normalized; None when `doc` has none."""
        return self.normalized(get(doc, self.pointer))

    def sort_key(self, value) -> tuple:
        return _ORDERINGS[self.order](value)
