"""benchx/paired-relative/v1 (comparator.md §4.4): mean relative difference over paired rounds."""

import math
import statistics

from ..evidence import Assessment, Method, change_label

# The noise is the standard error of the per-round differences, so a verdict needs
# at least two pairs, whatever `min_rounds` a caller asks for.
MIN_PAIRS = 2


class PairedRelative(Method):
    name = "benchx/paired-relative"
    version = "v1"
    dims = ("round", "side")
    requires = ("equal-attempts", "alternating-slots", "duplicate-points")
    defaults = {"k": 3.0, "min_rounds": 3}
    config_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["k", "min_rounds"],
        "properties": {"k": {"type": "number"}, "min_rounds": {"type": "integer"}},
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

    def describe(self, effect, details):
        shown = f"{effect['value']:+8.2%}" if effect else "       ?"
        noise = f"{details['noise']:7.2%}" if "noise" in details else "      ?"
        return f"effect {shown}  noise {noise}  rounds={details['rounds']}"


METHOD = PairedRelative()
