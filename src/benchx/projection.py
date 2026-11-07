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

import statistics
from dataclasses import dataclass

from .core import sha256_hex
from .core.hashing import canonical
from .key import Key, axis_order, canonical_uri, escape, get, normalize_remote  # noqa: F401

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


# -- estimators ----------------------------------------------------------------

_ESTIMATORS = {
    ("median", "benchx/median/v1"): statistics.median,
    ("mean", "benchx/mean/v1"): statistics.fmean,
    ("min", "benchx/min/v1"): min,
}


def estimate(estimator: dict, values: list[float]) -> float | None:
    """The estimator applied to an observation batch; None if the implementation has no such method."""
    method = _ESTIMATORS.get((estimator["name"], estimator.get("method_version")))
    return method(values) if method and values and estimator.get("input_level") == "observations" else None


def values(doc: dict) -> list[float]:
    return [o["value"] if isinstance(o, dict) else o for o in doc["measurement"].get("observations", [])]


# -- projections ---------------------------------------------------------------

def _has_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


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
        self.axis_keys = tuple(Key(path) for path in axis)
        self.axis = [key.pointer for key in self.axis_keys]
        self.default = policy["default"]
        self.paths = {**policy.get("paths", {}), **self.extra}
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

    def key(self, pointer: str) -> Key | None:
        """How a fact enters the identity: its role as a key, None when it is ignored."""
        role = self.role(pointer)
        if role == "ignore":
            return None
        return Key(pointer) if role == "match" else Key(pointer, normalize=role["normalize"])

    def apply(self, doc: dict, project: str | None = None) -> Projected:
        """`doc` under this projection. `project` stands in for the project of a
        thin result (schema §4.1), so local results of different hosts stay apart."""
        axis_paths = self.axis
        named = set(self.paths) | set(axis_paths)
        identity, ignored = {}, {}

        def place(pointer, value):
            key = self.key(pointer)
            if key is None:
                ignored[pointer] = value
            elif _has_null(value):
                # A null fact never equals anything (§4.3): this result shares no series.
                identity[pointer] = {"unmatchable": [doc["producer"]["name"], doc["ingest_key"]]}
            else:
                identity[pointer] = key.normalized(value)

        def walk(node, pointer):
            if pointer in axis_paths:
                return
            if isinstance(node, dict) and any(n.startswith(pointer + "/") for n in named):
                for key, value in node.items():
                    walk(value, f"{pointer}/{escape(key)}")
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
        return Projected(tuple(key.read(doc) for key in self.axis_keys), identity, ignored)

    # -- fingerprints (§4.3) --

    def comparison_fingerprint(self, projected: Projected) -> str:
        return _hash("benchmark-comparison-v1", self.identity_schema.encode(), canonical(projected.identity))

    def series_fingerprint(self, projected: Projected, quantity: str, estimator: dict) -> str:
        return _hash("benchmark-series-v6", self.comparison_fingerprint(projected).encode(), quantity.encode(),
                     _hash("benchmark-estimator-v1", canonical(estimator)).encode())

    def order_key(self, point_axis: tuple) -> tuple:
        """A sort key for a point: none for a revision axis, which the
        implementation orders by time since it has no revision graph (schema §4.1);
        otherwise the axis values in natural order (`axis_order`)."""
        return () if self.axis == ["/revision/key"] else tuple(
            key.sort_key(value) for key, value in zip(self.axis_keys, point_axis))


def reported_coordinates(doc: dict) -> dict:
    """Everything reported about what was measured, minus the axis revision and the quantity (§4.3)."""
    return {key: doc[key] for key in REPORTED if key in doc}


def reported_coordinates_fingerprint(doc: dict) -> str:
    return _hash("benchmark-reported-coordinates-v1", canonical(reported_coordinates(doc)))
