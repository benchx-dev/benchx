"""The comparator's framework (comparator.md §2): a selector forms the evidence, a
method assesses it, and this module checks what the method asked for and writes
the comparison document. It holds no statistics. The document depends only on the
inputs, so the same results give the same document from any store.

Only run mode is reachable from the CLI. History mode and detectors are specified
in comparator.md; a selector for them would call `evaluate` as run mode does.
"""

import json

from jsonschema import Draft202012Validator

from . import methods
from .core import catalog
from .evidence import SIDE, CompareError, Evidence, Layout, Method, NotApplicable, Selection, pivot
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
    failed.extend(selection.disagreements)
    if failed:
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
        doc["exclusions"].extend({"result": row.result, "reason": row.exclusion}
                                 for row in evidence.rows if row.exclusion)
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
    method, the invariant that says so and why."""
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
    unit = {**where, "series": evidence.series, **({"evidence": summary} if summary else {}),
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
    for section in ("regressed", "improved", "changed", "no-change-detected", "equivalent", "indeterminate"):
        rows = [u for u in doc["units"] if u["verdict"] == section]
        if not rows:
            continue
        lines.append(f"\n{section.replace('-', ' ').upper()} ({len(rows)})")
        for u in rows:
            reason = f"  ({u['reason']})" if "reason" in u else ""
            lines.append(f"  {workload_label(u['workload']):<32} {u['quantity']:<10} {_described(u)}{reason}")
    for key, title in (("missing", "missing"), ("failed_invariants", "failed invariants"),
                       ("exclusions", "excluded")):
        if doc[key]:
            lines.append(f"\n{title} ({len(doc[key])})")
            lines.extend(f"  {item}" for item in doc[key])
    return "\n".join(lines)
