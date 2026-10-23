"""Projections (schema §4.3): identity policies, which are documents
(schemas/identity-policy/0.1.0), and the projections code builds from them."""

import copy
import json

import pytest

from benchx import compare, core, identity
from benchx.core import catalog
from benchx.projection import Projection, ProjectionError

POLICIES = catalog.SCHEMAS / "identity-policy" / "0.1.0" / "examples"
DEFAULT = json.loads((POLICIES / "default.json").read_text())
ARROW = json.loads((catalog.SCHEMAS / "measurement-result" / "0.1.0" / "examples" / "arrow.json").read_text())


def history(policy=DEFAULT) -> Projection:
    return Projection(policy, ["/revision/key"])


def validate(policy) -> list:
    return list(catalog.fragment("identity-policy", "0.1.0", "").iter_errors(policy))


@pytest.mark.parametrize("file", sorted(POLICIES.glob("*.json")), ids=lambda path: path.stem)
def test_every_example_policy_is_valid(file):
    assert validate(json.loads(file.read_text())) == []


def test_the_prototype_policy_is_valid_and_pinned_to_its_name():
    """Any edit to the policy needs a new identity_schema (§4.3): this hash is
    the policy `benchx/prototype-identity/v1` names. Change both together."""
    assert validate(identity.POLICY_DATA) == []
    assert identity.IDENTITY_SCHEMA == "benchx/prototype-identity/v1"
    assert core.sha256_hex(core.canonical(identity.POLICY_DATA)) == \
        "c75cf9853f3b29b259c9f2658d337778237f21d874c96fd2adff0ea9c83259ea"


def test_adding_an_estimator_leaves_the_policy_and_its_series_alone():
    """Estimators are configured beside the identity policy, not in it (§4.5):
    another estimator adds series and changes none of the existing ones."""
    minimum = {"name": "min", "method_version": "benchx/min/v1", "input_level": "observations"}
    before = {p["series_fingerprint"] for p in identity.points(ARROW, "arrow")}
    after = identity.points(ARROW, "arrow", estimators=(*identity.ESTIMATORS, minimum))
    assert before < {p["series_fingerprint"] for p in after}
    assert [p["value"] for p in after if p["estimator"] == minimum] == [min(ARROW["measurement"]["observations"])]


def test_the_default_matches_what_it_does_not_name():
    """Fail-closed: a fact nobody named, here a new runtime fact, splits rather than blends."""
    other = copy.deepcopy(ARROW)
    other["coordinates"]["environment"]["system"]["runtime"]["jit"] = "on"
    assert history().apply(ARROW).identity != history().apply(other).identity


def test_the_default_ignores_its_named_system_facts():
    other = copy.deepcopy(ARROW)
    other["coordinates"]["environment"]["system"]["os"]["kernel"] = "6.9.0"
    a, b = history().apply(ARROW), history().apply(other)
    assert a.identity == b.identity
    assert b.ignored["/coordinates/environment/system/os"] == {"kernel": "6.9.0"}


def test_a_policy_normalizes_before_matching():
    arrow = json.loads((POLICIES / "arrow.json").read_text())
    assert history(arrow).apply(ARROW).identity["/benchmark/revision/key"] == "suite-v3"  # mapped
    other = copy.deepcopy(ARROW)
    other["source"]["uri"] = "https://GitHub.com/apache/arrow.git/"
    assert history().apply(ARROW).identity == history().apply(other).identity


def test_the_longest_path_wins_and_the_axis_wins_over_any_role():
    sweep = Projection(DEFAULT, ["/coordinates/environment/system/os/kernel"],
                       profile="test/kernel-sweep/v1", paths={"/provenance/subject_tree": "match"})
    applied = sweep.apply(ARROW)
    assert applied.axis == ("6.8.0-31-generic",)
    assert "/coordinates/environment/system/os/kernel" not in applied.ignored
    p = history({**DEFAULT, "paths": {"/coordinates/environment/system": "ignore",
                                      "/coordinates/environment/system/runtime": "match"}})
    assert "/coordinates/environment/system/runtime" in p.apply(ARROW).identity


def test_points_along_a_value_axis_are_in_natural_order():
    """Sizes by value and versions as people read them, not as JSON text,
    where 1000 sorts before 20 and kernel 6.10 before 6.8."""
    sizes = Projection(DEFAULT, ["/coordinates/workload/parameters/size"], profile="test/size-sweep/v1",
                       paths={"/provenance/subject_tree": "match"})
    assert sorted([1000, 20, 100, None, 2.5], key=lambda v: sizes.order_key((v,))) == [2.5, 20, 100, 1000, None]
    kernels = Projection(DEFAULT, ["/coordinates/environment/system/os/kernel"], profile="test/kernel-sweep/v1",
                         paths={"/provenance/subject_tree": "match"})
    found = ["6.10.0-1-generic", "6.8.0-31-generic", "6.8.0-4-generic", "5.15.0"]
    assert sorted(found, key=lambda v: kernels.order_key((v,))) == \
        ["5.15.0", "6.8.0-4-generic", "6.8.0-31-generic", "6.10.0-1-generic"]
    # Values of different kinds, and several axis parts, still order without error.
    two = Projection(DEFAULT, ["/coordinates/workload/parameters/role", "/coordinates/workload/parameters/size"],
                     profile="test/two-factor/v1", paths={"/provenance/subject_tree": "match"})
    points = [("variant", 100), ("baseline", 1000), ("baseline", 20), ("variant", "x"), ("baseline", {"n": 1})]
    assert sorted(points, key=two.order_key) == \
        [("baseline", 20), ("baseline", 1000), ("baseline", {"n": 1}), ("variant", 100), ("variant", "x")]


def test_null_never_equals_anything():
    """Schema §4.3: two results with a null fact share no series."""
    nulls = []
    for key in ("a", "b"):
        doc = copy.deepcopy(ARROW)
        doc["ingest_key"] = key
        doc["coordinates"]["environment"]["system"]["process"] = {"environment": None}
        nulls.append(doc)
    assert history().apply(nulls[0]).identity != history().apply(nulls[1]).identity


def test_named_only_facts_are_considered_only_when_named():
    assert "/provenance/subject_tree" not in history().apply(ARROW).identity
    assert "/provenance/subject_tree" in compare.PROFILES["environments"].apply(ARROW).identity


@pytest.mark.parametrize("axis, paths, message", [
    (["/coordinates/environment/system/os/kernel"], None, "must match /provenance/subject_tree"),
    (["/revision/key"], {"/provenance/run_key": "match"}, "not a reported coordinate"),
    (["/provenance/info/x"], None, "outside"),
])
def test_projections_that_break_a_rule_are_refused(axis, paths, message):
    with pytest.raises(ProjectionError, match=message):
        Projection(DEFAULT, axis, profile="test/v1", paths=paths)


def test_a_profile_is_named_after_itself_and_its_policy():
    """Both names enter comparison_fingerprint: editing either gives new series."""
    revisions = compare.PROFILES["revisions"]
    assert revisions.identity_schema == "benchx/profile/revisions/v1+benchx/prototype-identity/v1"
    a, b = history(), history({**DEFAULT, "identity_schema": "benchx/default/v2"})
    assert a.apply(ARROW).identity == b.apply(ARROW).identity
    assert a.series_fingerprint(a.apply(ARROW), "wall-time", identity.MEDIAN) != \
        b.series_fingerprint(b.apply(ARROW), "wall-time", identity.MEDIAN)


def test_the_quantity_is_never_a_coordinate():
    """Quantity is in the measurement: no role can ignore it, so it always splits series."""
    other = copy.deepcopy(ARROW)
    other["measurement"]["quantity"] = {"name": "cpu-time", "unit": "s"}
    assert history().apply(ARROW).identity == history().apply(other).identity
    assert identity.series_fingerprint(ARROW, "arrow", identity.MEDIAN, history()) != \
        identity.series_fingerprint(other, "arrow", identity.MEDIAN, history())
