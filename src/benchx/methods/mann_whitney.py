"""benchx/mann-whitney/v1 (comparator.md §4.5): unpaired rank test on attempt estimates.

Asks nothing of the run's schedule or pairing, so it applies to a run that did not
alternate; it ignores any blocking the run has, which is the cost of asking less.
"""

import math
import statistics
from math import comb

from ..evidence import Assessment, Method, change_label

# Attempts in total, up to which the p-value is exact.
EXACT_LIMIT = 40


def _doubled_midranks(pooled: list[float]) -> list[int]:
    """Twice each value's midrank (1-based), so that tied ranks stay integers."""
    order = sorted(range(len(pooled)), key=pooled.__getitem__)
    ranks = [0] * len(pooled)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and pooled[order[j + 1]] == pooled[order[i]]:
            j += 1
        for position in order[i:j + 1]:
            ranks[position] = i + j + 2  # (i + 1 + j + 1) / 2, doubled
        i = j + 1
    return ranks


def _exact_p(ranks: list[int], observed: int, m: int) -> float:
    """The two-sided p-value over every way to choose `m` of the pooled ranks: the
    share of choices whose rank sum is at least as far from its mean as `observed`.
    Counts by rank sum, so ties need no special handling."""
    counts = [{} for _ in range(m + 1)]
    counts[0][0] = 1
    for rank in ranks:
        for chosen in range(m - 1, -1, -1):
            for total, ways in counts[chosen].items():
                counts[chosen + 1][total + rank] = counts[chosen + 1].get(total + rank, 0) + ways
    center = m * (len(ranks) + 1)  # the mean of a doubled rank sum, in the same units
    extreme = sum(ways for total, ways in counts[m].items() if abs(total - center) >= abs(observed - center))
    return extreme / comb(len(ranks), m)


def _normal_p(pooled: list[float], u: float, n: int, m: int) -> float:
    """The tie-corrected normal approximation, with continuity correction."""
    total = n + m
    ties = sum(t ** 3 - t for t in (pooled.count(value) for value in set(pooled)))
    variance = n * m / 12 * (total + 1 - ties / (total * (total - 1)))
    if variance == 0:
        return 1.0
    z = max(abs(u - n * m / 2) - 0.5, 0) / math.sqrt(variance)
    return math.erfc(z / math.sqrt(2))


def rank_test(reference: list[float], target: list[float]) -> tuple[float, float, str]:
    """(U of the target, two-sided p-value, how the p-value was computed)."""
    n, m = len(reference), len(target)
    pooled = [*reference, *target]
    ranks = _doubled_midranks(pooled)
    observed = sum(ranks[n:])
    u = (observed - m * (m + 1)) / 2
    if n + m <= EXACT_LIMIT:
        return u, _exact_p(ranks, observed, m), "exact-permutation"
    return u, _normal_p(pooled, u, n, m), "normal-approximation"


class MannWhitney(Method):
    name = "benchx/mann-whitney"
    version = "v1"
    dims = ("side",)
    requires = ()
    defaults = {"alpha": 0.05}
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["alpha"],
        "properties": {"alpha": {"type": "number", "exclusiveMinimum": 0, "maximum": 1}},
    }
    details_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["attempts"],
        "properties": {
            "attempts": {"description": "Usable attempts per side, the reference side first",
                         "type": "array", "items": {"type": "integer", "minimum": 0}, "minItems": 2, "maxItems": 2},
            "smallest_p": {"description": "The smallest two-sided p-value this many attempts can reach",
                           "type": "number", "minimum": 0, "maximum": 1},
            "reference_estimate": {"description": "Median of the reference side's estimates", "type": "number"},
            "u": {"description": "Mann-Whitney U of the target side", "type": "number", "minimum": 0},
            "p_value": {"type": "number", "minimum": 0, "maximum": 1},
            "p_method": {"enum": ["exact-permutation", "normal-approximation"]},
        },
    }

    def assess(self, layout, evidence, config):
        reference, target = ([row for row in side if row.estimate is not None] for side in layout.cells)
        used = (*reference, *target)
        n, m = len(reference), len(target)
        excluded = tuple((row, "no-estimate") for side in layout.cells for row in side if row.estimate is None)
        details = {"attempts": [n, m]}

        def indeterminate(reason):
            return Assessment("indeterminate", reason=reason, used=used, excluded=excluded, details=details)

        if evidence.no_verdict:
            return indeterminate(evidence.no_verdict)
        if evidence.quantity.get("deterministic"):
            return indeterminate("deterministic-deferred")
        if not n or not m:
            return indeterminate("insufficient-attempts")
        details["smallest_p"] = min(1.0, 2 / comb(n + m, n))
        before = [row.estimate for row in reference]
        after = [row.estimate for row in target]
        center = statistics.median(before)
        details["reference_estimate"] = center
        # A design that cannot reach alpha would report no change whatever it measured.
        if details["smallest_p"] > config["alpha"]:
            return indeterminate("insufficient-attempts")
        u, p, how = rank_test(before, after)
        details.update(u=u, p_value=p, p_method=how)
        verdict = (change_label(u - n * m / 2, evidence.quantity.get("direction")) if p <= config["alpha"]
                   else "no-change-detected")
        effect = None if center == 0 else {"measure": "relative-median", "value": statistics.median(after) / center - 1}
        return Assessment(verdict, effect=effect, used=used, excluded=excluded, details=details)

    def describe(self, effect, details):
        shown = f"{effect['value']:+8.2%}" if effect else "       ?"
        p = f"{details['p_value']:7.4f}" if "p_value" in details else "      ?"
        return f"effect {shown}  p {p}  attempts={details['attempts'][0]}+{details['attempts'][1]}"


METHOD = MannWhitney()
