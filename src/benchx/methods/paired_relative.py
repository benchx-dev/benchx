"""benchx/paired-relative/v2 (comparator.md §4.4): mean relative difference over paired rounds."""

import functools
import logging
import math
import statistics

from ..evidence import NOT_COMPARABLE, Assessment, Method, change_label
from ..projection import workload_label

logger = logging.getLogger(__name__)

# The noise is the standard error of the per-round differences, so a verdict needs
# at least two pairs, whatever `min_rounds` a caller asks for.
MIN_PAIRS = 2
# The largest k: a normal tail of 20 sigma is 5e-89, far from where a double underflows.
MAX_K = 20


def _continued_fraction(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function, by the modified Lentz method."""
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        for numerator in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 + numerator * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + numerator / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return h


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _continued_fraction(a, b, x) / a
    return 1 - front * _continued_fraction(b, a, 1 - x) / b


def two_sided_tail(t: float, df: int) -> float:
    """P(|T| > t) for Student's t with `df` degrees of freedom, as I_{df/(df+t^2)}(df/2, 1/2),
    which keeps its precision far into the tail; so that scipy is not needed."""
    return _incomplete_beta(df / 2, 0.5, df / (df + t * t))


@functools.cache  # the same few (k, df) for every unit of a run
def multiplier(k: float, df: int) -> float:
    """The multiple of the standard error that `k` stands for at `df` degrees of freedom: the t
    quantile with the two-sided tail of a normal at k sigma (k = 3: 0.27%). It is 19.2 at 2 degrees
    of freedom, 5.5 at 5 and 4.1 at 9, and tends to k as they grow (comparator.md §4.3, #126)."""
    if k <= 0:
        return 0.0
    tail = math.erfc(k / math.sqrt(2))
    low, high = 0.0, max(k, 1.0)
    while two_sided_tail(high, df) > tail:
        high *= 2
    for _ in range(200):
        middle = (low + high) / 2
        low, high = (middle, high) if two_sided_tail(middle, df) > tail else (low, middle)
    return high


class PairedRelative(Method):
    name = "benchx/paired-relative"
    version = "v2"
    dims = ("round", "side")
    requires = ("equal-attempts", "alternating-slots", "duplicate-points")
    defaults = {"k": 3.0, "min_rounds": 4}
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["k", "min_rounds"],
        "properties": {"k": {"type": "number", "minimum": 0, "maximum": MAX_K}, "min_rounds": {"type": "integer"}},
    }
    details_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rounds", "noise_kind", "noise_source"],
        "properties": {
            "rounds": {"description": "Paired rounds", "type": "integer", "minimum": 0},
            "reference_estimate": {"description": "Median of the baseline's estimates", "type": "number"},
            "noise": {"type": "number", "minimum": 0},
            "noise_kind": {"const": "standard-error"},
            "noise_source": {"const": "paired-rounds"},
            "degrees_of_freedom": {"type": "integer", "minimum": 1},
            "multiplier": {
                "description": "The t quantile at degrees_of_freedom with the two-sided tail of a normal at k sigma",
                "type": "number",
                "minimum": 0,
            },
            "threshold": {"description": "multiplier times the noise", "type": "number", "minimum": 0},
        },
    }

    def assess(self, layout, evidence, config):
        pairs, excluded = [], []
        for baseline, contender in layout.cells:  # one per round, the sides in evidence order
            if baseline and contender and baseline[0].estimate is not None and contender[0].estimate is not None:
                pairs.append((baseline[0], contender[0]))
            else:
                excluded.extend((row, "unpaired") for row in (*baseline, *contender))
        used = tuple(row for pair in pairs for row in pair)
        label = f"{workload_label(evidence.workload)} [{evidence.quantity['name']}]"
        one_sided = [
            (r, "baseline" if baseline else "contender")
            for r, (baseline, contender) in zip(layout.coords.get("round", ()), layout.cells)
            if bool(baseline) != bool(contender)
        ]
        if one_sided and evidence.no_verdict not in NOT_COMPARABLE:
            logger.debug(
                "%s: %d round(s) paired, %d dropped for a success on one side only (%s)",
                label,
                len(layout.cells) - len(one_sided),
                len(one_sided),
                "; ".join(f"round {r} has {side} only" for r, side in one_sided),
            )
        details = {
            "rounds": sum(1 for baseline, contender in layout.cells if baseline and contender),
            "noise_kind": "standard-error",
            "noise_source": "paired-rounds",
        }

        def indeterminate(reason):
            return Assessment("indeterminate", reason=reason, used=used, excluded=tuple(excluded), details=details)

        if evidence.no_verdict:
            return indeterminate(evidence.no_verdict)
        estimates = [(b.estimate, c.estimate) for b, c in pairs]
        if estimates:
            details["reference_estimate"] = statistics.median(b for b, _ in estimates)

        if evidence.quantity.get("deterministic"):
            # Deferred: no quantity of the implementation is deterministic (comparator.md §4.4).
            return indeterminate("deterministic-deferred")
        if len(estimates) < max(config["min_rounds"], MIN_PAIRS):
            return indeterminate("insufficient-rounds")
        if any(b == 0 for b, _ in estimates):
            return indeterminate("zero-baseline")
        differences = [c / b - 1 for b, c in estimates]
        effect = statistics.fmean(differences)
        noise = statistics.stdev(differences) / math.sqrt(len(differences))
        df = len(differences) - 1
        times = multiplier(config["k"], df)
        threshold = times * noise
        details.update(noise=noise, degrees_of_freedom=df, multiplier=times, threshold=threshold)
        verdict = (
            "no-change-detected"
            if abs(effect) <= threshold
            else change_label(effect, evidence.quantity.get("direction"))
        )
        return Assessment(
            verdict,
            effect={"measure": "relative", "value": effect},
            used=used,
            excluded=tuple(excluded),
            details=details,
        )

    def configured(self, config):
        if config["min_rounds"] < MIN_PAIRS:
            logger.warning(
                "min_rounds %d is below %d, the fewest rounds a verdict needs: %d is used",
                config["min_rounds"],
                MIN_PAIRS,
                MIN_PAIRS,
            )

    def log_facts(self, unit, config):
        details = unit["details"]
        facts = [f"rounds {details['rounds']}"]
        if "reference_estimate" in details:
            facts.append(f"reference {details['reference_estimate']:.6g}")
        if "effect" in unit:
            facts += [
                f"effect {unit['effect']['value']:+.2%}",
                f"noise {details['noise']:.2%}",
                f"threshold {details['threshold']:.2%} ({details['multiplier']:.3g} x noise)",
            ]
        if unit.get("reason") == "insufficient-rounds":
            facts.append(f"{max(config['min_rounds'], MIN_PAIRS)} paired rounds required")
        return facts

    def describe(self, effect, details):
        shown = f"{effect['value']:+8.2%}" if effect else "       ?"
        noise = f"{details['noise']:7.2%}" if "noise" in details else "      ?"
        return f"effect {shown}  noise {noise}  rounds={details['rounds']}"


METHOD = PairedRelative()
