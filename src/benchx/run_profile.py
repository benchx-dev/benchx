"""The run-profile selector: two sides of one run, as a profile defines them (comparator.md §2.3).

A profile is a projection built here from the identity policy: its axis says what
tells the sides apart, the policy's roles what must match for two results to be
compared, and its invariants what makes the sides meaningful (schema §5.5). The
checks a method needs, such as alternation, are the method's to ask for. Units
are the profile projection's series.
"""

import json

from . import identity
from .core import canonical
from .evidence import COLUMNS, CompareError, Evidence, Row, Selection, Selector, result_ref, slot_schedule
from .key import escape, get
from .projection import Projection, estimate, values
from .snapshot import DIRTY_WARNINGS
from .store import store_project

PROFILES = {
    # UC-03: two subject trees from one run.
    "revisions": Projection(identity.POLICY_DATA, ["/provenance/subject_tree"],
                            profile="benchx/profile/revisions/v1",
                            invariants=["one-run", "two-sides", "one-tree-per-side", "one-benchmark-tree"]),
    # UC-04: one revision measured two ways. A side is what its subject
    # configuration, pinned components, and system are; the caller's label,
    # put in front of the axis per comparison, only names it.
    "environments": Projection(identity.POLICY_DATA, ["/coordinates/subject/configuration",
                                                      "/coordinates/subject/components",
                                                      "/coordinates/environment/system"],
                               profile="benchx/profile/environments/v1",
                               paths={"/revision/key": "match", "/provenance/subject_tree": "match"},
                               invariants=["one-run", "two-sides", "sides-differ"]),
}
WORKLOAD = "/coordinates/workload"
ROLES = ("baseline", "contender")


def _c(value) -> str:
    return canonical(value).decode()


def profile_projection(profile: str, label: str | None = None) -> Projection:
    """The profile's projection; for environments, with the label that names
    the sides at the front of its axis."""
    if profile not in PROFILES:
        raise CompareError(f"profile {profile!r} is not in the implementation ({', '.join(PROFILES)})")
    projection = PROFILES[profile]
    if profile == "environments":
        if not label:
            raise CompareError("the environments profile needs the label that names the sides")
        projection = projection.with_axis_first(f"/provenance/labels/{label}")
    return projection


def dotted(pointer: str) -> str:
    return pointer[1:].replace("/", ".")


def _measurand(doc) -> bool:
    """Whether the result is what its attempt set out to measure. A condition
    measurement (`measurement.kind: condition`, schema §4.4), such as the
    runner's load average, is kept beside the measurands and never compared."""
    return doc["measurement"].get("kind", "measurand") == "measurand"


def _failed(invariant: str, detail: str, **where) -> dict:
    return {"invariant": invariant, "owner": "selector", **where, "detail": detail}


class RunProfile(Selector):
    name = "benchx/run-profile"
    version = "v1"

    def select(self, documents, *, run_key, profile, baseline, label=None) -> Selection:
        projection = profile_projection(profile, label)
        documents = sorted((d for d in documents if d["provenance"].get("run_key") == run_key and _measurand(d)),
                           key=lambda d: (d["producer"]["name"], d["ingest_key"]))
        if not documents:
            raise CompareError(f"no results for run {run_key!r}")
        naming = projection.axis[0]
        varying = dotted(naming)
        params = {"run_key": run_key, "profile": profile, "baseline": baseline, **({"label": label} if label else {})}
        selection = Selection(
            selector={"name": self.name, "version": self.version, "params": params},
            estimator=dict(identity.MEDIAN), identity_schema=projection.identity_schema,
            local_only=any(d["provenance"][f] != "clean" for d in documents
                           for f in ("subject_dirty", "benchmark_dirty")),
            inputs=[result_ref(d) for d in documents],
            sides={"varying": varying, "baseline": baseline})
        projected = [projection.apply(d, store_project(d)) for d in documents]
        if any(p.axis[0] is None for p in projected):
            if profile == "revisions":
                # UC-03 §10: a side with no working-tree id (its dirty state is `unknown`) cannot be named.
                detail = (f"a result has no {varying}: its tree's dirty state is unknown, so the side cannot be told "
                          "apart from the other or checked for a change during the run. Make every file in the "
                          "tree readable, or git-ignore it")
            else:
                detail = (f"a result has no {varying}: give every bx bench call --label NAME=VALUE, "
                          "and name that label here")
            selection.failed.append(_failed("sides", detail))
            selection.stopped = True
            return selection

        # A side is a distinct axis value, named by its first part (a tree, a label).
        sides = [_c(p.axis) for p in projected]
        names = {side: p.axis[0] for side, p in zip(sides, projected)}
        if profile == "revisions":
            matches = {side for side, d in zip(sides, documents)
                       if names[side].startswith(baseline) or d["revision"]["key"].startswith(baseline)}
        else:
            matches = {side for side in names if names[side] == baseline}
        if len({_c(names[side]) for side in matches}) != 1:
            raise CompareError(f"baseline {baseline!r} matches {len(matches)} sides of "
                               f"{sorted(map(str, names.values()))}")
        base = sorted(matches)[0]  # several sides of one name fail two-sides below
        # The side, not how the caller abbreviated it: one comparison, one document.
        params["baseline"] = selection.sides["baseline"] = names[base]
        if len(names) != 2:
            selection.failed.append(_failed("two-sides", f"{len(names)} sides: "
                                                         f"{sorted(_c(n) for n in names.values())}"))
            selection.stopped = True
            return selection
        contender = next(side for side in names if side != base)
        selection.sides["contender"] = names[contender]
        selection.sides["declared"] = {role: _declared(next(d for d, x in zip(documents, sides) if x == side))
                                       for role, side in zip(ROLES, (base, contender))}
        selection.slots = slot_schedule((COLUMNS["slot"].read(d), s) for d, s in zip(documents, sides))
        selection.failed.extend(self._profile_invariants(projection, documents, names, sides, base, contender, varying))
        groups = self._series(projection, documents, projected, sides, base, contender)
        selection.disagreements.extend(self._disagreements(groups))
        selection.units.extend(self._unit(projection, group, base, contender)
                               for group in sorted(groups.values(), key=lambda g: (_c(_workload(g["docs"])), g["quantity"])))
        return selection

    def _profile_invariants(self, projection, documents, names, sides, base, contender, varying) -> list[dict]:
        """Run-level invariants (schema §5.5, comparator.md §3), as the profile names them."""
        check, failed = set(projection.invariants), []
        if "sides-differ" in check:
            a, b = (json.loads(side) for side in (base, contender))
            if all(x == y for path, x, y in zip(projection.axis, a, b) if not path.startswith("/provenance/")):
                failed.append(_failed("sides-differ",
                                      f"the sides differ only in their label ({varying}): nothing recorded about the "
                                      "environment differs. Describe what differs with a context collector "
                                      "(collectors in benchx.conf.json) or a setup file for each environment"))
        if "one-tree-per-side" in check:
            trees = {side: {d["provenance"].get("subject_tree") for d, x in zip(documents, sides) if x == side}
                     for side in (base, contender)}
            if any(len(t) != 1 or None in t for t in trees.values()) or trees[base] == trees[contender]:
                failed.append(_failed("one-tree-per-side", "each side must have one tree id, and the two must differ"))
        if "one-benchmark-tree" in check:
            failed.extend(_benchmark_file(documents, names, sides))
        return failed

    def _series(self, projection, documents, projected, sides, base, contender) -> dict:
        """Units are series: the results grouped by fingerprint under the profile's projection."""
        groups = {}
        for d, p, s in zip(documents, projected, sides):
            quantity = d["measurement"]["quantity"]["name"]
            fingerprint = projection.series_fingerprint(p, quantity, identity.MEDIAN)
            group = groups.setdefault(fingerprint, {"series": fingerprint, "quantity": quantity, "identity": p.identity,
                                                    "docs": {base: [], contender: []}})
            group["docs"][s].append(d)
        return groups

    def _disagreements(self, groups) -> list[dict]:
        """Several series for one workload variant and quantity mean the sides
        disagree on a matched fact (schema §5.5). A variant on one side only
        is one series, and merely missing; this is what tells the two apart."""
        variants = {}
        for group in groups.values():
            variant = {ptr: v for ptr, v in group["identity"].items()
                       if ptr == WORKLOAD or ptr.startswith(WORKLOAD + "/")}
            variants.setdefault((_c(variant), group["quantity"]), []).append(group)
        failed = []
        for (_, quantity), found in variants.items():
            if len(found) > 1:
                differing = _differing([g["identity"] for g in found])
                failed.append(_failed("sides-agree", f"{', '.join(differing)} differ",
                                      workload=_workload(found[0]["docs"]), quantity=quantity))
        return failed

    def _unit(self, projection, group, base, contender) -> Evidence:
        by_side = group["docs"]
        workload = _workload(by_side)
        rows = tuple(_row(d, role) for role, side in zip(ROLES, (base, contender)) for d in by_side[side])
        absent = tuple(role for role, side in zip(ROLES, (base, contender)) if not by_side[side])
        if absent:
            quantity = next(d for docs in by_side.values() for d in docs)["measurement"]["quantity"]
            return Evidence(group["series"], workload, quantity, ROLES, rows, missing=absent)
        first = {side: by_side[side][0] for side in (base, contender)}
        return Evidence(
            group["series"], workload, first[base]["measurement"]["quantity"], ROLES, rows,
            side_series={role: identity.series_fingerprint(first[side], store_project(first[side]), identity.MEDIAN)
                         for role, side in zip(ROLES, (base, contender))},
            unmatched_differences=tuple(_differences(projection, by_side[base], by_side[contender])),
            caveats=tuple(_caveats(by_side, base, contender)))


RUN_PROFILE = RunProfile()


def _row(doc: dict, side: str) -> Row:
    measurement = doc["measurement"]
    columns = {name: key.read(doc) for name, key in COLUMNS.items()}
    if measurement["status"] != "success":
        status, reason = measurement["status"], measurement.get("reason")
        return Row(result_ref(doc), side, columns, exclusion=f"{status}: {reason}" if reason else status)
    batch = values(doc)
    return Row(result_ref(doc), side, columns, estimate(identity.MEDIAN, batch), tuple(batch))


def _benchmark_file(documents, names, sides) -> list[dict]:
    """The `one-benchmark-tree` invariant (schema §5.5, UC-03 §10). A benchmark recorded apart from
    the subject has its own `benchmark_tree`, which differs from `subject_tree`; it must be one file
    on both sides and in every round. A benchmark in the subject's checkout has `benchmark_tree`
    equal to `subject_tree` and differs between the sides as the subject does, so it is not
    checked. A result without a `benchmark_tree` (a thin result) is outside the rule.

    An in-tree record must also name the subject's source as its benchmark's: one that does not is
    malformed. The reverse is not required, since a suite in another checkout of the same
    repository has the subject's source URI but its own file id."""
    found, malformed = [], []
    for d, side in zip(documents, sides):
        provenance = d["provenance"]
        tree = provenance.get("benchmark_tree")
        if tree is None:
            continue
        separate = tree != provenance.get("subject_tree")
        found.append((side, tree, separate))
        recorded = d.get("benchmark")
        if recorded is not None and not separate and _c(recorded.get("source")) != _c(d["source"]):
            malformed.append(str(names[side]))
    failed = []
    if malformed:
        failed.append(_failed("one-benchmark-tree",
                              "malformed record: the benchmark is in the subject's tree but names another "
                              f"source (sides {sorted(set(malformed))})"))
    if any(separate for _, _, separate in found):
        trees = {tree for _, tree, _ in found}
        if len(trees) != 1 or not all(separate for _, _, separate in found):
            failed.append(_failed("one-benchmark-tree",
                                  f"a benchmark recorded apart from the subject must be one file on both sides "
                                  f"and in every round: {len(trees)} benchmark trees "
                                  f"({', '.join(sorted(t[:12] for t in trees))})"))
    return failed


def _workload(by_side) -> dict:
    """The unit's workload variant, as the comparison document names it."""
    workload = next(d for docs in by_side.values() for d in docs)["coordinates"]["workload"]
    return {"name": workload["name"], **({"parameters": workload["parameters"]} if workload.get("parameters") else {})}


_ABSENT = object()


def _diff(pointer: str, found: list) -> list[str]:
    """The deepest pointers at which `found` are not all the same."""
    if all(isinstance(v, dict) for v in found):
        keys = sorted(set().union(*found))
        return [p for key in keys for p in _diff(f"{pointer}/{escape(key)}", [v.get(key, _ABSENT) for v in found])]
    distinct = {"<absent>" if v is _ABSENT else _c(v) for v in found}
    return [pointer] if len(distinct) > 1 else []


def _differing(facts: list[dict]) -> list[str]:
    """Pointers whose value is not the same in every one of `facts` (pointer -> value)."""
    return sorted(p for key in set().union(*facts) for p in _diff(key, [f.get(key, _ABSENT) for f in facts]))


def _declared(document) -> dict:
    """What a side says about how it was built and entered, beside what the
    runner saw: the declarations are recorded as given and never judged."""
    info = document["provenance"].get("info", {})
    side = {k: info[k] for k in ("how_built", "activation", "shell", "target_provider") if k in info}
    if isinstance(side.get("how_built"), dict):  # facts, shown as the JSON text they are
        side["how_built"] = json.dumps(side["how_built"], sort_keys=True, separators=(",", ":"))
    system = document["coordinates"]["environment"].get("system") or {}
    if env := (system.get("process") or {}).get("environment"):
        side["observed_env"] = env
    return side


def _caveats(by_side, base, contender) -> list[str]:
    """The warnings a side's results carry (quality.warnings), by side, for the reader."""
    found = set()
    for role, side in zip(ROLES, (base, contender)):
        for d in by_side[side]:
            for w in d.get("quality", {}).get("warnings", []):
                found.add(f"{role}: {w.get('code', w) if isinstance(w, dict) else w}")
            # A side that is not clean is named whether or not its producer warned (UC-03 §10).
            for flag, code in DIRTY_WARNINGS:
                if d["provenance"].get(flag) == "dirty":
                    found.add(f"{role}: {code}")
    return sorted(found)


def _differences(projection: Projection, a_docs, b_docs) -> list[str]:
    """What the projection does not match that differs between the sides: its
    ignored facts, and the parts of its axis after the first, which name what
    the sides differ in (schema §4.3)."""
    def facts(docs):
        found = []
        for d in docs:
            projected = projection.apply(d, store_project(d))
            found.append({**projected.ignored, **dict(list(zip(projection.axis, projected.axis))[1:])})
        return found

    def at(fact, pointer):
        for key, value in fact.items():
            if pointer == key or pointer.startswith(key + "/"):
                return get(value, pointer[len(key):]) if pointer != key else value
        return None

    def seen(found, pointer):
        return {_c(at(fact, pointer)) for fact in found}

    a, b = facts(a_docs), facts(b_docs)
    return [p for p in _differing(a + b) if seen(a, p) != seen(b, p)]
