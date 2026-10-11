"""Comparator, run mode only (comparator.md §2.3; prototype-design.md §5).

Two sides of one run, as the `revisions` or `environments` profile defines
them (schema §5.5), paired by round and compared with the method
benchx/paired-relative/v1. The document depends only on the input results,
so the same results give the same document from any store.
"""

import math
import statistics

from . import identity
from .core import canonical
from .store import store_project

METHOD = {"name": "benchx/paired-relative", "version": "v1", "estimator": "median"}
# Observed-context keys that change between any two attempts; not caveats.
VOLATILE = {"load_avg_1m", "google_benchmark"}


class CompareError(Exception):
    """The request itself cannot be answered, such as an unknown baseline."""


def _c(value) -> str:
    return canonical(value).decode()


def _sides(documents, profile, label):
    """Side of each document, the varying coordinate, and a failed invariant if any."""
    if profile == "revisions":
        if any("subject_tree" not in d["provenance"] for d in documents):
            return None, "provenance.subject_tree", "a side has no subject tree id"
        return [d["provenance"]["subject_tree"] for d in documents], "provenance.subject_tree", None
    if profile == "environments":
        if not label:
            raise CompareError("the environments profile needs the label that names the sides")
        key = label
        sides = [d["provenance"].get("labels", {}).get(key) for d in documents]
        if None in sides:
            return None, f"provenance.labels.{key}", f"a result has no label {key!r}"
        return sides, f"provenance.labels.{key}", None
    raise CompareError(f"profile {profile!r} is not in the prototype (revisions, environments)")


# Schema §5.5: what the two sides must agree on, as field paths.
_MUST_AGREE = {
    "revisions": ("coordinates.subject.configuration", "coordinates.subject.components",
                  "coordinates.environment.identity", "coordinates.comparison_context"),
    "environments": ("revision.key", "provenance.subject_tree", "coordinates.environment.identity",
                     "coordinates.comparison_context"),
}


def _get(doc, path):
    for part in path.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def _estimate(doc):
    """The attempt's median observation; the method's estimator (METHOD)."""
    values = [o["value"] if isinstance(o, dict) else o for o in doc["measurement"].get("observations", [])]
    return statistics.median(values) if values else None


def _verdict(effect, noise, k, direction):
    if abs(effect) <= k * noise:
        return "no change detected"
    if direction == "lower-is-better":
        return "regressed" if effect > 0 else "improved"
    if direction == "higher-is-better":
        return "improved" if effect > 0 else "regressed"
    return "changed"


def compare(documents, *, run_key, profile, baseline, label=None, k=3.0, min_rounds=3) -> dict:
    documents = sorted((d for d in documents if d["provenance"].get("run_key") == run_key),
                       key=lambda d: (d["producer"]["name"], d["ingest_key"]))
    if not documents:
        raise CompareError(f"no results for run {run_key!r}")
    sides, varying, problem = _sides(documents, profile, label)
    doc = {"comparison_version": 1, "mode": "run", "run_key": run_key, "profile": profile,
           "varying": varying, "baseline": baseline, "contender": None,
           "method": {**METHOD, "k": k, "min_rounds": min_rounds},
           "identity_schema": identity.IDENTITY_SCHEMA,
           "local_only": any(d["provenance"][f] != "clean" for d in documents
                             for f in ("subject_dirty", "benchmark_dirty")),
           "units": [], "missing": [], "failed_invariants": [], "exclusions": [],
           "inputs": [{"producer": d["producer"]["name"], "ingest_key": d["ingest_key"]} for d in documents]}
    if problem:
        doc["failed_invariants"].append({"invariant": "sides", "detail": problem})
        return doc

    values = sorted(set(sides))
    if profile == "revisions":
        matches = {s for s, d in zip(sides, documents)
                   if s.startswith(baseline) or d["revision"]["key"].startswith(baseline)}
    else:
        matches = {s for s in values if s == baseline}
    if len(matches) != 1:
        raise CompareError(f"baseline {baseline!r} matches {len(matches)} sides of {values}")
    base = matches.pop()
    doc["baseline"] = base
    if len(values) != 2:
        doc["failed_invariants"].append({"invariant": "two-sides", "detail": f"{len(values)} sides: {values}"})
        return doc
    contender = next(v for v in values if v != base)
    doc["contender"] = contender
    doc["sides"] = {str(s): _declared(next(d for d, x in zip(documents, sides) if x == s))
                    for s in (base, contender)}

    # Run-level invariants (schema §5.5, comparator.md §3).
    for path in _MUST_AGREE[profile]:
        seen = {_c(_get(d, path)) for d in documents}
        if len(seen) > 1:
            doc["failed_invariants"].append({"invariant": "sides-agree", "detail": f"{path} differs"})
    if profile == "environments":
        by_side = {s: {_c(d["coordinates"]["subject"]) for d, x in zip(documents, sides) if x == s}
                   for s in values}
        if by_side[base] == by_side[contender]:
            doc["failed_invariants"].append({"invariant": "sides-differ",
                                             "detail": "the labeled sides report the same subject"})
    if profile == "revisions":
        # UC-03 §10: a dirty or unknown tree cannot be validated (schema §5.5).
        unclean = sorted({s for d, s in zip(documents, sides)
                          if any(d["provenance"].get(f) != "clean" for f in ("subject_dirty", "benchmark_dirty"))})
        if unclean:
            doc["failed_invariants"].append({"invariant": "clean-tree",
                                             "detail": f"sides with a dirty or unknown tree: {unclean}"})
    slots = {}
    for d, s in zip(documents, sides):
        slot = d.get("procedure", {}).get("slot")
        if slot is None:
            doc["failed_invariants"].append({"invariant": "alternation", "detail": "a result has no slot"})
            break
        slots.setdefault(slot, set()).add(s)
    else:
        order = [slots[slot] for slot in sorted(slots)]
        if any(len(o) != 1 for o in order) or any(a == b for a, b in zip(order, order[1:])):
            doc["failed_invariants"].append({"invariant": "alternation",
                                             "detail": "sides do not alternate by procedure.slot"})
    if doc["failed_invariants"]:
        return doc

    # Units: one workload variant and quantity each.
    units = {}
    for d, s in zip(documents, sides):
        c = d["coordinates"]
        key = (c["workload"]["name"], _c(c["workload"].get("parameters", {})), c["quantity"]["name"])
        units.setdefault(key, {base: [], contender: []})[s].append(d)
    for (workload, parameters, quantity), by_side in sorted(units.items()):
        where = {"workload": workload, "parameters": parameters, "quantity": quantity}
        absent = [s for s in (base, contender) if not by_side[s]]
        if absent:
            doc["missing"].extend({**where, "side": s} for s in absent)
            continue
        if len(by_side[base]) != len(by_side[contender]):
            doc["failed_invariants"].append({"invariant": "equal-attempts", **where,
                                             "detail": f"{len(by_side[base])} against {len(by_side[contender])}"})
            continue
        rounds = {}
        for s in (base, contender):
            for d in by_side[s]:
                if d["measurement"]["status"] != "success":
                    doc["exclusions"].append({"ingest_key": d["ingest_key"], "status": d["measurement"]["status"],
                                              "reason": d["measurement"].get("reason")})
                    continue
                rounds.setdefault(d.get("procedure", {}).get("round"), {})[s] = d
        pairs = [(r[base], r[contender]) for _, r in sorted(rounds.items(), key=lambda x: (x[0] is None, x[0]))
                 if base in r and contender in r]
        doc["units"].append(_unit(where, pairs, by_side, base, contender, k, min_rounds))
    return doc


def _declared(document) -> dict:
    """What a side says about how it was built and entered, beside what the
    runner saw: the declarations are recorded as given and never judged."""
    info = document["provenance"].get("info", {})
    side = {k: info[k] for k in ("how_built", "activation", "shell", "target_provider") if k in info}
    if env := document.get("observed_context", {}).get("env"):
        side["observed_env"] = env
    return side


def _unit(where, pairs, by_side, base, contender, k, min_rounds):
    first = {s: by_side[s][0] for s in (base, contender)}
    quantity = first[base]["coordinates"]["quantity"]
    unit = {**where, "series": {name: identity.series_fingerprint(first[s], store_project(first[s]), identity.MEDIAN)
                                for name, s in (("baseline", base), ("contender", contender))},
            "rounds": len(pairs), "reference_estimate": None, "effect": None, "noise": None,
            "noise_source": "paired-rounds", "verdict": "indeterminate", "reason": None,
            "observed_context_differences": _differences(by_side[base], by_side[contender])}
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


def _differences(a_docs, b_docs):
    def values(docs):
        found = {}
        for d in docs:
            for key, value in d.get("observed_context", {}).items():
                if key not in VOLATILE:
                    found.setdefault(key, set()).add(_c(value))
        return found

    a, b = values(a_docs), values(b_docs)
    return sorted(key for key in set(a) | set(b) if a.get(key) != b.get(key))


def render(doc: dict) -> str:
    """The reviewer-speed table (comparator.md §5)."""
    lines = [f"run {doc['run_key']}  profile {doc['profile']}  "
             f"baseline {str(doc['baseline'])[:12]}  contender {str(doc['contender'])[:12]}"
             + ("  [local-only]" if doc["local_only"] else "")]
    for name, side in doc.get("sides", {}).items():
        for key in ("how_built", "activation", "observed_env"):
            if key in side:
                shown = side[key] if key != "observed_env" else ", ".join(f"{k}={v}" for k, v in side[key].items())
                first, *rest = str(shown).splitlines() or [""]
                lines.append(f"  {name}: {key.replace('_', ' ')}: {first}" + (f" (+{len(rest)} lines)" if rest else ""))
    for section in ("regressed", "improved", "changed", "no change detected", "indeterminate"):
        rows = [u for u in doc["units"] if u["verdict"] == section]
        if not rows:
            continue
        lines.append(f"\n{section.upper()} ({len(rows)})")
        for u in rows:
            effect = f"{u['effect']:+8.2%}" if u["effect"] is not None else "       ?"
            noise = f"{u['noise']:7.2%}" if u["noise"] is not None else "      ?"
            reason = f"  ({u['reason']})" if u["reason"] else ""
            lines.append(f"  {u['workload']:<32} {u['quantity']:<10} effect {effect}  noise {noise}"
                         f"  rounds={u['rounds']}{reason}")
    for key, title in (("missing", "missing"), ("failed_invariants", "failed invariants"),
                       ("exclusions", "excluded")):
        if doc[key]:
            lines.append(f"\n{title} ({len(doc[key])})")
            lines.extend(f"  {item}" for item in doc[key])
    return "\n".join(lines)
