"""Projections: how stored results become series (schema §4.3).

A projection decides which results form a series: it names an axis, the facts
a series follows, and gives every other reported fact a role, match,
normalize, or ignore. Applied to one result it gives the result's axis value,
its comparison identity (the matched facts, normalized), and the facts it
ignored. The identity policy (schemas/identity-policy/0.1.0) is a project's
default projection: its axis is always the subject revision, so its document
stores only the roles. A comparison profile reuses those roles with the
coordinate it varies as the axis. Only the identity policy is data; other
projections are built in code. Nothing here reads a store.
"""

import re
import statistics
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .core import sha256_hex
from .core.hashing import canonical

# The reported coordinates (schema §4.3), which every role applies to, and the
# facts outside them a projection may name: considered only when it does.
REPORTED = ("project", "source", "benchmark", "coordinates")
NAMED_ONLY = ("/revision/key", "/provenance/subject_tree")
LABELS = "/provenance/labels/"
CODE = ("/revision/key", "/provenance/subject_tree")


class ProjectionError(Exception):
    """A projection that breaks a rule of §4.3."""


def _hash(prefix: str, *parts: bytes) -> str:
    return sha256_hex(prefix.encode() + b"\0" + b"\0".join(parts))


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


# -- estimators ----------------------------------------------------------------

_ESTIMATORS = {
    ("median", "benchx/median/v1"): statistics.median,
    ("mean", "benchx/mean/v1"): statistics.fmean,
    ("min", "benchx/min/v1"): min,
}


def estimate(estimator: dict, values: list[float]) -> float | None:
    """The estimator applied to an observation batch; None if the prototype has no such method."""
    method = _ESTIMATORS.get((estimator["name"], estimator.get("method_version")))
    return method(values) if method and values and estimator.get("input_level") == "observations" else None


def values(doc: dict) -> list[float]:
    return [o["value"] if isinstance(o, dict) else o for o in doc["measurement"].get("observations", [])]


# -- pointers ------------------------------------------------------------------

def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def get(doc: dict, pointer: str):
    """The value at a JSON Pointer, or None when any step is missing."""
    for part in pointer.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def _has_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


# -- projections ---------------------------------------------------------------

@dataclass(frozen=True)
class Projected:
    """One result under a projection."""
    axis: tuple            # one value per axis path; None where the result has none
    identity: dict         # pointer -> matched value, normalized: the comparison identity
    ignored: dict          # pointer -> ignored value


class Projection:
    """Roles from an identity policy, an axis, and one versioned name.

    `policy` is an identity policy document. `axis` lists the JSON Pointers a
    series follows. `profile` names a comparison profile built on the policy;
    the projection's `identity_schema`, which enters comparison_fingerprint, is
    then the profile's name joined to the policy's, so editing either gives new
    series. `paths` adds roles for facts outside the policy, such as matching
    the subject tree, and `invariants` are the checks a profile runs (§5.5).
    """

    def __init__(self, policy: dict, axis: list[str], *, profile: str | None = None,
                 paths: dict | None = None, invariants=()):
        self.policy, self.profile, self.extra = policy, profile, dict(paths or {})
        self.identity_schema = (f"{profile}+{policy['identity_schema']}" if profile
                                else policy["identity_schema"])
        self.axis = list(axis)
        self.default = policy["default"]
        self.paths = {**policy.get("paths", {}), **self.extra}
        self.estimators = policy.get("estimators", [])
        self.invariants = list(invariants)
        for path in self.paths:
            if path not in NAMED_ONLY and path.split("/")[1] not in REPORTED:
                raise ProjectionError(f"{self.identity_schema}: {path} is not a reported coordinate (§4.3)")
        for path in self.axis:
            if path not in NAMED_ONLY and path.split("/")[1] not in REPORTED and not (
                    path.startswith(LABELS) and path.count("/") == 3):
                raise ProjectionError(f"{self.identity_schema}: the axis path {path} is outside what §4.3 allows")
        if not (set(self.axis) & set(CODE) or self.paths.get("/provenance/subject_tree") == "match"):
            raise ProjectionError(f"{self.identity_schema}: an axis that is not the code must match "
                                  "/provenance/subject_tree (§4.3)")

    def with_axis_first(self, path: str) -> "Projection":
        """This projection with one more axis path in front, such as the label
        that names the sides of a comparison."""
        return Projection(self.policy, [path, *self.axis], profile=self.profile, paths=self.extra,
                          invariants=self.invariants)

    def role(self, pointer: str):
        """The role of a fact: the longest named path at or above it, else the default."""
        for path in sorted((p for p in self.paths if pointer == p or pointer.startswith(p + "/")),
                           key=len, reverse=True):
            return self.paths[path]
        return self.default

    def apply(self, doc: dict, project: str | None = None) -> Projected:
        """`doc` under this projection. `project` stands in for the project of a
        thin result (schema §4.1), so local results of different hosts stay apart."""
        axis_paths = self.axis
        named = set(self.paths) | set(axis_paths)
        identity, ignored = {}, {}

        def place(pointer, value):
            role = self.role(pointer)
            if role == "ignore":
                ignored[pointer] = value
            elif _has_null(value):
                # A null fact never equals anything (§4.3): this result shares no series.
                identity[pointer] = {"unmatchable": [doc["producer"]["name"], doc["ingest_key"]]}
            else:
                identity[pointer] = value if role == "match" else _normalize(role["normalize"], value)

        def walk(node, pointer):
            if pointer in axis_paths:
                return
            if isinstance(node, dict) and any(n.startswith(pointer + "/") for n in named):
                for key, value in node.items():
                    walk(value, f"{pointer}/{_escape(key)}")
            else:
                place(pointer, node)

        reported = {key: doc[key] for key in REPORTED if key in doc}
        if project is not None:
            reported["project"] = project
        for key, value in reported.items():
            walk(value, f"/{key}")
        for pointer in NAMED_ONLY:
            if pointer in self.paths and pointer not in axis_paths:
                place(pointer, get(doc, pointer))
        return Projected(tuple(get(doc, path) for path in axis_paths), identity, ignored)

    # -- fingerprints (§4.3) --

    def comparison_fingerprint(self, projected: Projected) -> str:
        return _hash("benchmark-comparison-v1", self.identity_schema.encode(), canonical(projected.identity))

    def series_fingerprint(self, projected: Projected, quantity: str, estimator: dict) -> str:
        return _hash("benchmark-series-v7", self.comparison_fingerprint(projected).encode(), quantity.encode(),
                     _hash("benchmark-estimator-v1", canonical(estimator)).encode())

    def order_key(self, point_axis: tuple) -> tuple:
        """A sort key for a point: none for a revision axis, which the
        prototype orders by time since it has no revision graph (schema §4.1);
        otherwise the axis value's canonical form."""
        return () if self.axis == ["/revision/key"] else (canonical(list(point_axis)).decode(),)


def reported_coordinates(doc: dict) -> dict:
    """Everything reported about what was measured, minus the axis revision and the quantity (§4.3)."""
    return {key: doc[key] for key in REPORTED if key in doc}


def reported_coordinates_fingerprint(doc: dict) -> str:
    return _hash("benchmark-reported-coordinates-v2", canonical(reported_coordinates(doc)))
