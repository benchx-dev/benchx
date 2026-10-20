"""Context collectors (harness-adapter.md §5.1), and the check against declared facts.

A collector is a command the order names. The runner runs it once, before the
cases, in the harness's working directory and environment, outside any timed
region and under a time limit. It prints one JSON object of facts. The facts go
to the artifacts in full, and to every result's `environment.system.<name>` as their
`summary` object (if they have one) and their sha256.

A collector that fails, times out, or prints anything else adds no facts and
gives the results the warning `collector-failed`; nothing is substituted.

A collector may also be held to what was declared: `verify: how_built` requires
every key of the target's `how_built` object to be among the collected facts
with exactly the same value, and `expect` requires facts by value. A failed
check refuses the order, or with `on_fail: warn` gives the results the warning
`collector-mismatch`. A collector that cannot run cannot verify, so under
`refuse` that refuses too.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import core
from .core import Refusal

DEFAULT_TIMEOUT_S = 60.0
MAX_DIFFS = 5


@dataclass
class Collected:
    system: dict = field(default_factory=dict)  # name -> what goes into environment.system
    artifacts: list = field(default_factory=list)  # (kind, media_type, path)
    warnings: list = field(default_factory=list)
    refusals: list = field(default_factory=list)


def _differences(wanted, found, path=""):
    """The paths at which `found` does not contain `wanted` exactly. A mapping is
    compared key by key (extra keys in `found` are fine); anything else by value."""
    if isinstance(wanted, dict):
        if not isinstance(found, dict):
            yield f"{path or '.'}: expected an object, found {_short(found)}"
            return
        for key, value in wanted.items():
            if key not in found:
                yield f"{path}/{key}: missing (declared {_short(value)})"
            else:
                yield from _differences(value, found[key], f"{path}/{key}")
    elif wanted != found:
        yield f"{path or '.'}: declared {_short(wanted)}, observed {_short(found)}"


def _short(value, limit=80) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "..."


def _collect(spec, cwd, env) -> tuple[dict | None, str | None]:
    """The facts a collector prints, or (None, why not)."""
    try:
        done = subprocess.run(spec["command"], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=spec.get("timeout_s", DEFAULT_TIMEOUT_S))
    except OSError as e:
        return None, f"cannot run {spec['command'][0]!r}: {e}"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {spec.get('timeout_s', DEFAULT_TIMEOUT_S):g} s"
    if done.returncode != 0:
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        return None, f"exit {done.returncode}: {last}"
    try:
        facts = json.loads(done.stdout)
    except ValueError:
        return None, "printed something other than a JSON object"
    if not isinstance(facts, dict):
        return None, "printed something other than a JSON object"
    return facts, None


def run_all(specs, *, declared, cwd, env, artifacts_dir, taken=()) -> Collected:
    """Run each collector. `declared` is the target's how_built, `taken` the
    environment.system names already in use."""
    out = Collected()
    for spec in specs:
        name = spec["name"]
        if name in taken or name in out.system:
            out.refusals.append(Refusal(f"collector {name!r}: that name is already in environment.system"))
            continue
        checks = bool(spec.get("verify") or spec.get("expect"))
        refuse = spec.get("on_fail", "refuse") == "refuse"
        facts, why = _collect(spec, cwd, env)
        problems = []
        if facts is None:
            out.warnings.append("collector-failed")
            problems.append(f"collector {name!r} failed: {why}")
        else:
            digest = core.sha256_hex(core.canonical(facts))
            path = Path(artifacts_dir) / f"collector-{name}.json"
            path.write_bytes(core.pretty(facts))
            out.artifacts.append(("collector-output", "application/json", path))
            summary = facts.get("summary") if isinstance(facts.get("summary"), dict) else {}
            out.system[name] = {**summary, "sha256": digest}
            if spec.get("verify") == "how_built":
                if isinstance(declared, dict):
                    problems += [f"collector {name!r}: how_built {d}" for d in _differences(declared, facts)]
                else:
                    problems.append(f"collector {name!r}: verify: how_built, but the target's how_built is not an object")
            problems += [f"collector {name!r}: expect {d}" for d in _differences(spec.get("expect", {}), facts)]
            if problems:
                out.warnings.append("collector-mismatch")
        if problems and checks and refuse:  # an unchecked collector that fails only warns
            out.refusals += [Refusal(problem) for problem in problems[:MAX_DIFFS]]
    return out
