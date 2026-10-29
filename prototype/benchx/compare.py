"""Comparator, run mode only (comparator.md §2.3; prototype-design.md §5).

Two sides of one run, as a profile defines them, paired by round and compared
with the method benchx/paired-relative/v1. A profile is a projection built
here from the identity policy: its axis says what tells the sides apart, the
policy's roles what must match for two results to be compared, and its
invariants what the run must satisfy (schema §5.5). The document depends only
on the input results, so the same results give the same document from any
store.
"""

import json
import math
import statistics

from . import identity
from .core import canonical
from .projection import Projection, get
from .store import store_project

METHOD = {"name": "benchx/paired-relative", "version": "v1", "effect_measure": "relative"}
PROFILES = {
    # UC-03: two subject trees from one run.
    "revisions": Projection(identity.POLICY_DATA, ["/provenance/subject_tree"],
                            profile="benchx/profile/revisions/v1",
                            invariants=["one-run", "two-sides", "one-tree-per-side", "equal-attempts",
                                        "alternating-slots", "clean-tree"]),
    # UC-04: one revision measured two ways. A side is what its subject
    # configuration, pinned components, and system are; the caller's label,
    # put in front of the axis per comparison, only names it.
    "environments": Projection(identity.POLICY_DATA, ["/coordinates/subject/configuration",
                                                      "/coordinates/subject/components",
                                                      "/coordinates/environment/system"],
                               profile="benchx/profile/environments/v1",
                               paths={"/revision/key": "match", "/provenance/subject_tree": "match"},
                               invariants=["one-run", "two-sides", "sides-differ", "equal-attempts",
                                           "alternating-slots"]),
}
WORKLOAD = "/coordinates/workload"


class CompareError(Exception):
    """The request itself cannot be answered, such as an unknown baseline."""


def _c(value) -> str:
    return canonical(value).decode()


def profile_projection(profile: str, label: str | None = None) -> Projection:
    """The profile's projection; for environments, with the label that names
    the sides at the front of its axis."""
    if profile not in PROFILES:
        raise CompareError(f"profile {profile!r} is not in the prototype ({', '.join(PROFILES)})")
    projection = PROFILES[profile]
    if profile == "environments":
        if not label:
            raise CompareError("the environments profile needs the label that names the sides")
        projection = projection.with_axis_first(f"/provenance/labels/{label}")
    return projection


def _dotted(pointer: str) -> str:
    return pointer[1:].replace("/", ".")


def _measurand(doc) -> bool:
    """Whether the result is what its attempt set out to measure. A condition
    measurement (`measurement.kind: condition`, schema §4.4), such as the
    runner's load average, is kept beside the measurands and never compared."""
    return doc["measurement"].get("kind", "measurand") == "measurand"


def _estimate(doc):
    """The attempt's median observation; the method's estimator (METHOD)."""
    values = [o["value"] if isinstance(o, dict) else o for o in doc["measurement"].get("observations", [])]
    return statistics.median(values) if values else None


def _verdict(effect, noise, k, direction):
    if abs(effect) <= k * noise:
        return "no-change-detected"
    if direction == "lower-is-better":
        return "regressed" if effect > 0 else "improved"
    if direction == "higher-is-better":
        return "improved" if effect > 0 else "regressed"
    return "changed"


def compare(documents, *, run_key, profile, baseline, label=None, k=3.0, min_rounds=3) -> dict:
    projection = profile_projection(profile, label)
    documents = sorted((d for d in documents if d["provenance"].get("run_key") == run_key and _measurand(d)),
                       key=lambda d: (d["producer"]["name"], d["ingest_key"]))
    if not documents:
        raise CompareError(f"no results for run {run_key!r}")
    naming = projection.axis[0]
    varying = _dotted(naming)
    doc = {"comparison_version": 1, "mode": "run", "run_key": run_key, "profile": profile,
           "varying": varying, "baseline": baseline, "pairing": "procedure.round",
           "method": {**METHOD, "configuration": {"k": k, "min_rounds": min_rounds}},
           "estimator": dict(identity.MEDIAN), "identity_schema": projection.identity_schema,
           "local_only": any(d["provenance"][f] != "clean" for d in documents
                             for f in ("subject_dirty", "benchmark_dirty")),
           "units": [], "missing": [], "failed_invariants": [], "exclusions": [],
           "inputs": [{"producer": d["producer"]["name"], "ingest_key": d["ingest_key"]} for d in documents]}
    failed = doc["failed_invariants"]
    projected = [projection.apply(d, store_project(d)) for d in documents]
    if any(p.axis[0] is None for p in projected):
        failed.append({"invariant": "sides", "detail": f"a result has no {varying}: give every bx bench call "
                                                       "--label NAME=VALUE, and name that label here"})
        return doc

    # A side is a distinct axis value, named by its first part (a tree, a label).
    sides = [_c(p.axis) for p in projected]
    names = {side: p.axis[0] for side, p in zip(sides, projected)}
    if profile == "revisions":
        matches = {side for side, d in zip(sides, documents)
                   if names[side].startswith(baseline) or d["revision"]["key"].startswith(baseline)}
    else:
        matches = {side for side in names if names[side] == baseline}
    if len({_c(names[side]) for side in matches}) != 1:
        raise CompareError(f"baseline {baseline!r} matches {len(matches)} sides of {sorted(map(str, names.values()))}")
    base = sorted(matches)[0]  # several sides of one name fail two-sides below
    doc["baseline"] = names[base]
    if len(names) != 2:
        failed.append({"invariant": "two-sides",
                       "detail": f"{len(names)} sides: {sorted(_c(n) for n in names.values())}"})
        return doc
    contender = next(side for side in names if side != base)
    doc["contender"] = names[contender]
    doc["sides"] = {role: _declared(next(d for d, x in zip(documents, sides) if x == side))
                    for role, side in (("baseline", base), ("contender", contender))}

    # Run-level invariants (schema §5.5, comparator.md §3), as the profile names them.
    check = set(projection.invariants)
    if "sides-differ" in check:
        a, b = (json.loads(side) for side in (base, contender))
        if all(x == y for path, x, y in zip(projection.axis, a, b) if not path.startswith("/provenance/")):
            failed.append({"invariant": "sides-differ",
                           "detail": f"the sides differ only in their label ({varying}): nothing recorded about the "
                                     "environment differs. Describe what differs with a context collector "
                                     "(collectors in benchx.conf.json) or a setup file for each environment"})
    if "one-tree-per-side" in check:
        trees = {side: {d["provenance"].get("subject_tree") for d, x in zip(documents, sides) if x == side}
                 for side in (base, contender)}
        if any(len(t) != 1 or None in t for t in trees.values()) or trees[base] == trees[contender]:
            failed.append({"invariant": "one-tree-per-side", "detail": "each side must have one tree id, and "
                                                                      "the two must differ"})
    if "clean-tree" in check:
        # UC-03 §10: a dirty or unknown tree cannot be validated (schema §5.5).
        unclean = sorted({str(names[s]) for d, s in zip(documents, sides)
                          if any(d["provenance"].get(f) != "clean" for f in ("subject_dirty", "benchmark_dirty"))})
        if unclean:
            failed.append({"invariant": "clean-tree", "detail": f"sides with a dirty or unknown tree: {unclean}"})
    if "alternating-slots" in check:
        slots = {}
        for d, s in zip(documents, sides):
            slot = d.get("procedure", {}).get("slot")
            if slot is None:
                failed.append({"invariant": "alternating-slots",
                               "detail": "a result has no slot: give every bx bench call --slot"})
                break
            slots.setdefault(slot, set()).add(s)
        else:
            order = [slots[slot] for slot in sorted(slots)]
            if any(len(o) != 1 for o in order) or any(a == b for a, b in zip(order, order[1:])):
                failed.append({"invariant": "alternating-slots",
                               "detail": "sides do not alternate by procedure.slot: run the sides in the same "
                                         "order every round (a, b, a, b, ...)"})

    # Units: one workload variant and quantity each. Everything else the
    # projection matches must agree across both sides (schema §5.5).
    units = {}
    for d, p, s in zip(documents, projected, sides):
        variant = {ptr: v for ptr, v in p.identity.items() if ptr == WORKLOAD or ptr.startswith(WORKLOAD + "/")}
        key = (_c(variant), d["measurement"]["quantity"]["name"])
        units.setdefault(key, {"docs": {base: [], contender: []}, "identities": []})
        units[key]["docs"][s].append(d)
        units[key]["identities"].append(p.identity)
    for (_, quantity), unit in units.items():
        differing = _differing(unit["identities"])
        if differing:
            failed.append({"invariant": "sides-agree", "workload": _workload(unit["docs"]), "quantity": quantity,
                           "detail": f"{', '.join(differing)} differ"})
    if failed:
        return doc

    for (_, quantity), unit in sorted(units.items(), key=lambda item: (_c(_workload(item[1]["docs"])), item[0][1])):
        by_side = unit["docs"]
        where = {"workload": _workload(by_side), "quantity": quantity}
        absent = [role for role, s in (("baseline", base), ("contender", contender)) if not by_side[s]]
        if absent:
            doc["missing"].extend({**where, "side": role} for role in absent)
            continue
        if "equal-attempts" in check and len(by_side[base]) != len(by_side[contender]):
            failed.append({"invariant": "equal-attempts", **where,
                           "detail": f"{len(by_side[base])} against {len(by_side[contender])}: run the same "
                                     "number of rounds on both sides"})
            continue
        rounds = {}
        for s in (base, contender):
            for d in by_side[s]:
                if d["measurement"]["status"] != "success":
                    status, reason = d["measurement"]["status"], d["measurement"].get("reason")
                    result = {"producer": d["producer"]["name"], "ingest_key": d["ingest_key"]}
                    doc["exclusions"].append({"result": result, "reason": f"{status}: {reason}" if reason else status})
                    continue
                rounds.setdefault(d.get("procedure", {}).get("round"), {})[s] = d
        pairs = [(r[base], r[contender]) for _, r in sorted(rounds.items(), key=lambda x: (x[0] is None, x[0]))
                 if base in r and contender in r]
        unit_doc = _unit(where, pairs, by_side, base, contender, k, min_rounds)
        unit_doc["unmatched_differences"] = _differences(projection, by_side[base], by_side[contender])
        if caveats := _caveats(by_side, base, contender):
            unit_doc["caveats"] = caveats
        doc["units"].append(unit_doc)
    return doc


def _workload(by_side) -> dict:
    """The unit's workload variant, as the comparison document names it."""
    workload = next(d for docs in by_side.values() for d in docs)["coordinates"]["workload"]
    return {"name": workload["name"], **({"parameters": workload["parameters"]} if workload.get("parameters") else {})}


_ABSENT = object()


def _diff(pointer: str, values: list) -> list[str]:
    """The deepest pointers at which `values` are not all the same."""
    if all(isinstance(v, dict) for v in values):
        keys = sorted(set().union(*values))
        return [p for key in keys for p in _diff(f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}",
                                                 [v.get(key, _ABSENT) for v in values])]
    distinct = {"<absent>" if v is _ABSENT else _c(v) for v in values}
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
    for role, side in (("baseline", base), ("contender", contender)):
        for d in by_side[side]:
            for w in d.get("quality", {}).get("warnings", []):
                found.add(f"{role}: {w.get('code', w) if isinstance(w, dict) else w}")
    return sorted(found)


def _unit(where, pairs, by_side, base, contender, k, min_rounds):
    first = {s: by_side[s][0] for s in (base, contender)}
    quantity = first[base]["measurement"]["quantity"]
    unit = {**where, "series": {name: identity.series_fingerprint(first[s], store_project(first[s]), identity.MEDIAN)
                                for name, s in (("baseline", base), ("contender", contender))},
            "rounds": len(pairs), "noise_source": "paired-rounds", "verdict": "indeterminate"}
    estimates = [(_estimate(b), _estimate(c)) for b, c in pairs]
    estimates = [(b, c) for b, c in estimates if b is not None and c is not None]
    if estimates:
        unit["reference_estimate"] = statistics.median(b for b, _ in estimates)
    if quantity.get("deterministic"):
        # Deferred: no prototype quantity is deterministic (prototype-design.md §5).
        unit["reason"] = "deterministic-deferred"
        return unit
    if len(estimates) < min_rounds:
        unit["reason"] = "insufficient-rounds"
        return unit
    if any(b == 0 for b, _ in estimates):
        unit["reason"] = "zero-baseline"
        return unit
    d = [c / b - 1 for b, c in estimates]
    effect = statistics.fmean(d)
    noise = statistics.stdev(d) / math.sqrt(len(d))
    unit.update(effect=effect, noise=noise, verdict=_verdict(effect, noise, k, quantity.get("direction")))
    return unit


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


def render(doc: dict) -> str:
    """The reviewer-speed table (comparator.md §5)."""
    lines = [f"run {doc['run_key']}  profile {doc['profile']}  "
             f"baseline {str(doc['baseline'])[:12]}  contender {str(doc.get('contender'))[:12]}"
             + ("  [local-only]" if doc["local_only"] else "")]
    for role, side in doc.get("sides", {}).items():
        name = doc[role]
        for key in ("how_built", "activation", "observed_env"):
            if key in side:
                shown = side[key] if key != "observed_env" else ", ".join(f"{k}={v}" for k, v in side[key].items())
                first, *rest = str(shown).splitlines() or [""]
                lines.append(f"  {name}: {key.replace('_', ' ')}: {first}" + (f" (+{len(rest)} lines)" if rest else ""))
    differs = sorted({p for u in doc["units"] for p in u.get("unmatched_differences", [])})
    if differs:
        shown = ", ".join(_dotted(p) for p in differs[:6])
        lines.append(f"  differs: {shown}" + (f" (+{len(differs) - 6} more)" if len(differs) > 6 else ""))
    caveats = sorted({c for u in doc["units"] for c in u.get("caveats", [])})
    if caveats:
        lines.append(f"  caveats: {', '.join(caveats)}")
    for section in ("regressed", "improved", "changed", "no-change-detected", "indeterminate"):
        rows = [u for u in doc["units"] if u["verdict"] == section]
        if not rows:
            continue
        lines.append(f"\n{section.replace('-', ' ').upper()} ({len(rows)})")
        for u in rows:
            effect = f"{u['effect']:+8.2%}" if "effect" in u else "       ?"
            noise = f"{u['noise']:7.2%}" if "noise" in u else "      ?"
            reason = f"  ({u['reason']})" if "reason" in u else ""
            lines.append(f"  {u['workload']['name']:<32} {u['quantity']:<10} effect {effect}  noise {noise}"
                         f"  rounds={u['rounds']}{reason}")
    for key, title in (("missing", "missing"), ("failed_invariants", "failed invariants"),
                       ("exclusions", "excluded")):
        if doc[key]:
            lines.append(f"\n{title} ({len(doc[key])})")
            lines.extend(f"  {item}" for item in doc[key])
    return "\n".join(lines)
