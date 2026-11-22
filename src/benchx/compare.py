"""The comparator's framework (comparator.md §2): a selector forms the evidence, a
method assesses it, and this module checks what the method asked for and writes
the comparison document. It holds no statistics. The document depends only on the
inputs, so the same results give the same document from any store.

Only run mode is reachable from the CLI. History mode and detectors are specified
in comparator.md; a selector for them would call `evaluate` as run mode does.
"""

import collections
import json

from jsonschema import Draft202012Validator

from . import methods
from .core import catalog
from .evidence import NOT_COMPARABLE, SIDE, CompareError, Evidence, Layout, Method, NotApplicable, Selection, pivot
from .projection import workload_label
from .run_profile import PROFILES, RUN_PROFILE, _declared, dotted, infer_parameter, profile_projection  # noqa: F401


def compare(documents, *, run_key, profile, baseline, label=None, parameter=None, method=methods.DEFAULT,
            **config) -> dict:
    """Two sides of one run under a method. `label` names the sides of `environments`, and
    `parameter` those of `variants`. Any other keyword is the method's configuration, such
    as `k` and `min_rounds` for the default."""
    chosen = methods.get(method)
    configuration = _configure(chosen, config)
    selection = RUN_PROFILE.select(documents, run_key=run_key, profile=profile, baseline=baseline, label=label,
                                   parameter=parameter)
    return evaluate(selection, chosen, configuration)


def _configure(method: Method, config: dict) -> dict:
    configuration = {**method.defaults, **config}
    errors = [e.message for e in Draft202012Validator(method.config_schema).iter_errors(configuration)]
    if errors:
        raise CompareError(f"{method.id}: {'; '.join(errors)}")
    return configuration


def evaluate(selection: Selection, method: Method, configuration: dict) -> dict:
    """The comparison document for what a selector selected, under a method."""
    doc = {"schema_version": catalog.COMPARISON_DOCUMENT, "selector": selection.selector,
           "method": {"name": method.name, "version": method.version, "configuration": configuration},
           "estimator": selection.estimator, "identity_schema": selection.identity_schema,
           "local_only": selection.local_only,
           **({"sides": selection.sides} if selection.sides else {}),
           "units": [], "missing": [], "failed_invariants": list(selection.failed), "exclusions": [],
           "inputs": selection.inputs}
    failed = doc["failed_invariants"]
    if selection.stopped:
        return doc
    if "alternating-slots" in method.requires:
        failed.extend(_alternation(selection.slots, selection.advice.get("alternating-slots", "")))
    run_failed = bool(failed)  # an invariant of the run: no unit is compared
    failed.extend(selection.disagreements)  # each about one unit, which the selector left out; the others go on
    if run_failed:
        return doc

    for evidence in selection.units:
        where = {"workload": evidence.workload, "quantity": evidence.quantity["name"]}
        if evidence.missing:
            doc["missing"].extend({**where, "side": side} for side in evidence.missing)
            continue
        layout, problem = _lay_out(evidence, method)
        if problem:
            failed.append({"invariant": problem[0], **where, "detail": problem[1]})
            continue
        if evidence.no_verdict not in NOT_COMPARABLE:  # a unit not compared lists nothing that went wrong in it
            doc["exclusions"].extend({"result": row.result, "reason": row.exclusion}  # results that did not measure
                                     for row in evidence.rows if row.exclusion and row.status != "success")
        doc["units"].append(_unit(selection, evidence, layout, method, configuration, where))
    return doc


def _alternation(slots: str | None, advice: str) -> list[dict]:
    if slots == "absent":
        detail = "a result has no slot: give every bx bench call --slot"
    elif slots == "not-alternating":
        detail = ("sides do not alternate by procedure.slot: run the sides in the same order every round "
                  "(a, b, a, b, ...)")
    else:
        return []
    return [{"invariant": "alternating-slots", "detail": detail + advice}]


def _lay_out(evidence: Evidence, method: Method) -> tuple[Layout | None, tuple[str, str] | None]:
    """This unit in the shape the method asked for; or, when it cannot go to this
    method, the invariant that says so and why. A unit whose results are not of one
    benchmark (`NOT_COMPARABLE`) has nothing to line up, so nothing is checked."""
    if evidence.no_verdict in NOT_COMPARABLE:
        return pivot(evidence, method.dims, lined_up=False), None
    if "equal-attempts" in method.requires:
        counts = [sum(1 for row in evidence.rows if row.side == side) for side in evidence.sides]
        if len(set(counts)) > 1:
            return None, ("equal-attempts", (f"{' against '.join(map(str, counts))}: run the same number of rounds "
                                             "on both sides"))
    try:
        layout = pivot(evidence, method.dims)
    except NotApplicable as e:
        return None, ("layout", f"{method.id} does not apply: {e}")
    if "duplicate-points" in method.requires and any(len(leaf) > 1 for leaf in layout.leaves()):
        return None, ("duplicate-points", (f"more than one usable result at the same {', '.join(method.dims)}: "
                                           "the comparator never picks one implicitly"))
    return layout, None


def _unit(selection: Selection, evidence: Evidence, layout: Layout, method: Method, configuration: dict,
          where: dict) -> dict:
    assessment = method.assess(layout, evidence, configuration)
    Draft202012Validator(method.details_schema).validate(assessment.details)
    if evidence.no_verdict and (assessment.verdict, assessment.reason, assessment.effect) != (
            "indeterminate", evidence.no_verdict, None):
        raise AssertionError(f"{method.id} gave a verdict on a unit its selector says has none "
                             f"({evidence.no_verdict}); comparator.md §3")
    summary = {**({"target": list(evidence.target)} if evidence.target else {}),
               **({"reference": list(evidence.reference)} if evidence.reference else {}),
               **({"ordering": evidence.ordering} if evidence.ordering else {}),
               **({"boundaries": list(evidence.boundaries)} if evidence.boundaries else {})}
    caveats = list(evidence.caveats)
    if selection.slots == "not-alternating" and "alternating-slots" not in method.requires:
        caveats = sorted({*caveats, "sides-did-not-alternate"})
    # How each side's attempts ended, by status: the evidence behind a reason such as `not-measured`.
    attempts = {side: dict(sorted(collections.Counter(row.status for row in evidence.rows if row.side == side).items()))
                for side in evidence.sides}
    unit = {**where, **({"series": evidence.series} if evidence.series else {}), "attempts": attempts,
            **({"evidence": summary} if summary else {}),
            "method": {"name": method.name, "version": method.version}, "verdict": assessment.verdict,
            **_partition(evidence, assessment, method.dims), "details": assessment.details}
    if assessment.reason:
        unit["reason"] = assessment.reason
    if assessment.effect:
        unit["effect"] = assessment.effect
    if evidence.unmatched_differences is not None:
        unit["unmatched_differences"] = list(evidence.unmatched_differences)
    if caveats:
        unit["caveats"] = caveats
    return unit


def _partition(evidence: Evidence, assessment, dims: tuple) -> dict:
    """Every result of the unit in exactly one of used and excluded, in table order."""
    def ref(row):
        return row.result["producer"], row.result["ingest_key"]

    def unused(row):
        lacking = [dim for dim in dims if dim != SIDE and row.columns.get(dim) is None]
        return f"no {lacking[0]}" if lacking else "not-used"

    used = {ref(row) for row in assessment.used}
    reasons = {ref(row): reason for row, reason in assessment.excluded}
    found = {"used": [], "excluded": []}
    for row in evidence.rows:
        if ref(row) in used:
            found["used"].append(row.result)
        else:
            found["excluded"].append({"result": row.result,
                                      "reason": row.exclusion or reasons.get(ref(row)) or unused(row)})
    return found


def _described(unit: dict) -> str:
    """A unit's numbers as its method words them, or plainly when the method is not in this implementation."""
    name = f"{unit['method']['name']}/{unit['method']['version']}"
    if name in methods.METHODS:
        return methods.METHODS[name].describe(unit.get("effect"), unit["details"])
    effect = f"{unit['effect']['value']:+.4g}" if "effect" in unit else "?"
    return f"effect {effect}  {json.dumps(unit['details'], sort_keys=True)}"


def _few(names, shown=3, count=False) -> str:
    """The first `shown` names; `, …` when there are more, and with `count` how many."""
    more = len(names) - shown
    return ", ".join(names[:shown]) + ((f", … (+{more} more)" if count else ", …") if more > 0 else "")


def _failed(items) -> list[str]:
    """Failed invariants in words, `invariant: detail`: the unit it affects first when it affects one, and
    units that failed alike as one line, `(K units: a, b, c, …)`."""
    groups = {}
    for item in items:
        label = workload_label(item["workload"]) if "workload" in item else None
        groups.setdefault((item["invariant"], item["detail"]), []).append(label)
    lines = []
    for (invariant, detail), labels in groups.items():
        units = list(dict.fromkeys(label for label in labels if label))
        text = f"{invariant}: {detail}"
        lines.append(f"{units[0]}: {text}" if len(units) == 1 else
                     f"{text}  ({len(units)} units: {_few(units)})" if units else text)
    return lines


def _reason(unit) -> str:
    """The unit's reason; for `not-measured`, which side measured nothing, and how its attempts ended:
    `not-measured: contender skipped`, or `baseline error 2, skipped 1` when the statuses differ."""
    if unit["reason"] == "benchmark-changed":
        return "benchmark-changed: its version differs between the results"
    if unit["reason"] != "not-measured":
        return unit["reason"]
    sides = []
    for role, statuses in unit["attempts"].items():
        if "success" not in statuses:
            many = len(statuses) > 1
            sides.append(f"{role} " + ", ".join(f"{status} {n}" if many else status
                                               for status, n in sorted(statuses.items(), key=lambda x: (-x[1], x[0]))))
    return f"not-measured: {', '.join(sides)}"


def _excluded(items) -> list[str]:
    """Results left out, one line for each reason (which has what the result said): `reason  (K results)`.
    Which results is in the document (`--json`), not here."""
    counts = {}
    for item in items:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    return [f"{reason}  ({n} result{'s' if n != 1 else ''})" for reason, n in counts.items()]


def _missing(items) -> list[str]:
    """Units a side has no result for, one line for each side: `not on contender: a, b, c, … (+N more)`."""
    sides = {}
    for item in items:
        sides.setdefault(item["side"], []).append(workload_label(item["workload"]))
    return [f"not on {side}: {_few(list(dict.fromkeys(labels)), count=True)}" for side, labels in sorted(sides.items())]


def render(doc: dict) -> str:
    """The reviewer-speed table (comparator.md §5)."""
    params, sides = doc["selector"]["params"], doc.get("sides", {})
    if sides:
        header = (f"run {params['run_key']}  profile {params['profile']}  "
                  f"baseline {str(sides['baseline'])[:12]}  contender {str(sides.get('contender'))[:12]}")
    else:  # a selector with sides of its own, such as reference and target
        header = "  ".join([doc["selector"]["name"], *(f"{k} {v}" for k, v in params.items())])
    lines = [header + ("  [local-only]" if doc["local_only"] else "")]
    for role, side in sides.get("declared", {}).items():
        name = sides[role]
        for key in ("how_built", "activation", "observed_env"):
            if key in side:
                shown = side[key] if key != "observed_env" else ", ".join(f"{k}={v}" for k, v in side[key].items())
                first, *rest = str(shown).splitlines() or [""]
                lines.append(f"  {name}: {key.replace('_', ' ')}: {first}" + (f" (+{len(rest)} lines)" if rest else ""))
    differs = sorted({p for u in doc["units"] for p in u.get("unmatched_differences", [])})
    if differs:
        shown = ", ".join(dotted(p) for p in differs[:6])
        lines.append(f"  differs: {shown}" + (f" (+{len(differs) - 6} more)" if len(differs) > 6 else ""))
    caveats = sorted({c for u in doc["units"] for c in u.get("caveats", [])})
    if caveats:
        lines.append(f"  caveats: {', '.join(caveats)}")
    for section in ("regressed", "improved", "changed", "no-change-detected", "equivalent", "indeterminate",
                    "not-comparable"):
        # As asv sorts them: a unit whose results are not of one benchmark is "not comparable", not indeterminate.
        rows = [u for u in doc["units"] if (section == "not-comparable") == (u.get("reason") in NOT_COMPARABLE)
                and (section == "not-comparable" or u["verdict"] == section)]
        if not rows:
            continue
        lines.append(f"\n{section.replace('-', ' ').upper()} ({len(rows)})")
        for u in rows:
            if section == "not-comparable":  # no effect, noise or rounds to show: say why, as asv does
                lines.append(f"  {workload_label(u['workload']):<32} {u['quantity']:<10} ({_reason(u)})")
                continue
            reason = f"  ({_reason(u)})" if "reason" in u else ""
            lines.append(f"  {workload_label(u['workload']):<32} {u['quantity']:<10} {_described(u)}{reason}")
    for title, items, say in (("missing", doc["missing"], _missing),
                              ("failed invariants", doc["failed_invariants"], _failed),
                              ("excluded", doc["exclusions"], _excluded)):
        if items:
            lines.append(f"\n{title} ({len(items)})")
            lines.extend(f"  {line}" for line in say(items))
    return "\n".join(lines)
