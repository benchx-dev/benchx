"""Fingerprints, the prototype identity policy, and series points (schema §4.3, §4.5).

The identity policy is the default projection, with the subject revision as
its axis; its roles are a document (schemas/identity-policy/0.1.0), and the
prototype has one, here. It is the default of the schema's examples with the
benchmark's revision ignored: the prototype runs only benchmarks in the
subject's own checkout, so that revision is the subject revision, the axis,
and matching it would split every series at every commit.
"""

from .projection import (Projection, normalize_remote, reported_coordinates_fingerprint,  # noqa: F401
                         estimate, values)

# Any change to POLICY_DATA other than its description needs a new
# identity_schema; tests/test_projection.py pins the two together.
POLICY_DATA = {
    "identity_schema": "benchx/prototype-identity/v2",
    "default": "match",
    "paths": {
        "/source/uri": {"normalize": {"rule": "canonical-uri"}},
        "/benchmark/source/uri": {"normalize": {"rule": "canonical-uri"}},
        "/benchmark/revision": "ignore",
        "/coordinates/environment/system/hostname": "ignore",
        "/coordinates/environment/system/os": "ignore",
        "/coordinates/environment/system/libc": "ignore",
        "/coordinates/environment/system/cpu": "ignore",
        "/coordinates/environment/system/gpu": "ignore",
        "/coordinates/environment/system/firmware": "ignore",
        "/coordinates/environment/system/container": "ignore",
        "/coordinates/environment/system/process/cgroup": "ignore",
    },
    "estimators": [{"name": "median", "method_version": "benchx/median/v1", "input_level": "observations"}],
}
POLICY = Projection(POLICY_DATA, ["/revision/key"])
IDENTITY_SCHEMA = POLICY.identity_schema
MEDIAN = {"name": "median", "method_version": "benchx/median/v1", "input_level": "observations"}


def comparison_identity(doc: dict, project: str, projection: Projection = POLICY) -> dict:
    return projection.apply(doc, project).identity


def series_fingerprint(doc: dict, project: str, estimator: dict, projection: Projection = POLICY) -> str:
    return projection.series_fingerprint(projection.apply(doc, project), doc["measurement"]["quantity"]["name"],
                                         estimator)


def points(doc: dict, project: str, projection: Projection = POLICY) -> list[dict]:
    """The series points a result contributes (§4.5): each of the projection's
    estimators the prototype can derive from its observations, and each
    producer estimate it carries."""
    if doc["measurement"]["status"] not in ("success", "partial"):
        return []
    projected = projection.apply(doc, project)
    batch, found = values(doc), []
    for estimator in projection.estimators:
        if (value := estimate(estimator, batch)) is not None:
            found.append((estimator, value, "server"))
    for summary in doc["measurement"].get("summaries", []):
        if summary["type"] == "estimate":
            found.append((summary["estimator"], summary["value"], "producer"))
    quantity = doc["measurement"]["quantity"]["name"]
    return [{"series_fingerprint": projection.series_fingerprint(projected, quantity, estimator),
             "estimator": estimator, "value": value, "source": source,
             "axis": projected.axis, "identity": projected.identity, "ignored": projected.ignored}
            for estimator, value, source in found]
