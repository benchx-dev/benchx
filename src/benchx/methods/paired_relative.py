"""benchx/paired-relative/v1 (comparator.md §4.4): mean relative difference over paired rounds."""

import logging
import math
import statistics

from ..evidence import NOT_COMPARABLE, Assessment, Method, change_label
from ..projection import workload_label

logger = logging.getLogger(__name__)

# The noise is the standard error of the per-round differences, so a verdict needs
# at least two pairs, whatever `min_rounds` a caller asks for.
MIN_PAIRS = 2


class PairedRelative(Method):
    name = "benchx/paired-relative"
    version = "v1"
    dims = ("round", "side")
    requires = ("equal-attempts", "alternating-slots", "duplicate-points")
    defaults = {"k": 3.0, "min_rounds": 4}
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["k", "min_rounds"],
        "properties": {"k": {"type": "number", "minimum": 0}, "min_rounds": {"type": "integer"}},
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
            "threshold": {"description": "k times the noise", "type": "number", "minimum": 0},
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
        one_sided = [(r, "baseline" if baseline else "contender") for r, (baseline, contender)
                     in zip(layout.coords.get("round", ()), layout.cells) if bool(baseline) != bool(contender)]
        if one_sided and evidence.no_verdict not in NOT_COMPARABLE:
            logger.debug("%s: %d round(s) paired, %d dropped for a success on one side only (%s)", label,
                         len(layout.cells) - len(one_sided), len(one_sided),
                         "; ".join(f"round {r} has {side} only" for r, side in one_sided))
        details = {"rounds": sum(1 for baseline, contender in layout.cells if baseline and contender),
                   "noise_kind": "standard-error", "noise_source": "paired-rounds"}

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
        threshold = config["k"] * noise
        details.update(noise=noise, degrees_of_freedom=len(differences) - 1, threshold=threshold)
        verdict = ("no-change-detected" if abs(effect) <= threshold
                   else change_label(effect, evidence.quantity.get("direction")))
        return Assessment(verdict, effect={"measure": "relative", "value": effect}, used=used,
                          excluded=tuple(excluded), details=details)

    def configured(self, config):
        if config["min_rounds"] < MIN_PAIRS:
            logger.warning("min_rounds %d is below %d, the fewest rounds a verdict needs: %d is used",
                           config["min_rounds"], MIN_PAIRS, MIN_PAIRS)

    def log_facts(self, unit, config):
        details = unit["details"]
        facts = [f"rounds {details['rounds']}"]
        if "reference_estimate" in details:
            facts.append(f"reference {details['reference_estimate']:.6g}")
        if "effect" in unit:
            facts += [f"effect {unit['effect']['value']:+.2%}", f"noise {details['noise']:.2%}",
                      f"k x noise {details['threshold']:.2%}"]
        if unit.get("reason") == "insufficient-rounds":
            facts.append(f"{max(config['min_rounds'], MIN_PAIRS)} paired rounds required")
        return facts

    def describe(self, effect, details):
        shown = f"{effect['value']:+8.2%}" if effect else "       ?"
        noise = f"{details['noise']:7.2%}" if "noise" in details else "      ?"
        return f"effect {shown}  noise {noise}  rounds={details['rounds']}"


METHOD = PairedRelative()
