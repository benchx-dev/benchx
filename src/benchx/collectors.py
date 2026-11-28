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
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import core, log
from .core import Refusal, loader

DEFAULT_TIMEOUT_S = 60.0
MAX_DIFFS = 5
MAX_LOGGED_DIFFS = 40  # the log's cap, like its other lists; a refusal keeps MAX_DIFFS

logger = logging.getLogger(__name__)


@dataclass
class Collected:
    system: dict = field(default_factory=dict)  # name -> what goes into environment.system
    artifacts: list = field(default_factory=list)  # (kind, media_type, path)
    warnings: list = field(default_factory=list)
    refusals: list = field(default_factory=list)


def _differences(wanted, found, path=""):
    """The paths at which `found` does not contain `wanted` exactly. A mapping is
    compared key by key (extra keys in `found` are fine); anything else by its
    canonical JSON, so `true` is not `1` as Python's `==` would have it."""
    if isinstance(wanted, dict):
        if not isinstance(found, dict):
            yield f"{path or '.'}: expected an object, found {_short(found)}"
            return
        for key, value in wanted.items():
            if key not in found:
                yield f"{path}/{key}: missing (declared {_short(value)})"
            else:
                yield from _differences(value, found[key], f"{path}/{key}")
    elif core.canonical(wanted) != core.canonical(found):
        yield f"{path or '.'}: declared {_short(wanted)}, observed {_short(found)}"


def _short(value, limit=80) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "..."


def _collect(spec, cwd, env) -> tuple[dict | None, str | None]:
    """The facts a collector prints, or (None, why not)."""
    name, limit = spec.get("name", "?"), spec.get("timeout_s", DEFAULT_TIMEOUT_S)
    logger.debug("collector %s: %s (in %s, limit %g s)", name, log.command(spec["command"]), cwd, limit)
    started = time.monotonic()
    try:
        done = subprocess.run(spec["command"], cwd=cwd, env=env, capture_output=True, text=True, timeout=limit)
        status, stdout, stderr = done.returncode, done.stdout, done.stderr
    except OSError as e:
        return None, f"cannot run {spec['command'][0]!r}: {e}"
    except subprocess.TimeoutExpired as expired:  # the partial output comes back as bytes
        status, stdout, stderr = None, log.as_text(expired.stdout), log.as_text(expired.stderr)
    logger.debug(
        "collector %s: %s in %.1f s",
        name,
        "timed out" if status is None else f"exit {status}",
        time.monotonic() - started,
    )
    failed = status is None or status != 0
    log.harness_output(logger, f"collector {name}", "stderr", stderr, failed=failed)
    if failed:  # on success stdout is the facts, which are kept as the collector-output artifact
        log.harness_output(logger, f"collector {name}", "stdout", stdout, failed=True)
    if status is None:
        return None, f"timed out after {limit:g} s"
    if status != 0:
        last = (stderr.strip().splitlines() or ["no message"])[-1]
        return None, f"exit {status}: {last}"
    facts, errors = loader.parse(stdout)
    if facts is None:
        return None, "; ".join(str(error) for error in errors)
    return facts, None


def run_all(specs, *, declared, cwd, env, artifacts_dir, taken=()) -> Collected:
    """Run each collector. `declared` is the target's how_built, `taken` the
    environment.system names already in use."""
    out = Collected()
    for number, spec in enumerate(specs, 1):
        name = spec["name"]
        if name in taken or name in out.system:
            out.refusals.append(Refusal(f"collector {name!r}: that name is already in environment.system"))
            continue
        checks = bool(spec.get("verify") or spec.get("expect"))
        refuse = spec.get("on_fail", "refuse") == "refuse"
        started = time.monotonic()
        facts, why = _collect(spec, cwd, env)
        took = time.monotonic() - started
        problems = []
        if facts is None:
            out.warnings.append("collector-failed")
            problems.append(f"collector {name!r} failed: {why}")
            logger.info("collector %d/%d %s: failed, %.1f s", number, len(specs), name, took)
            logger.warning(
                "collector %s failed: %s", name, why
            )  # even when it only warns: the reason is lost otherwise
        else:
            logger.info("collector %d/%d %s: ok, %.1f s", number, len(specs), name, took)
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
                    problems.append(
                        f"collector {name!r}: verify: how_built, but the target's how_built is not an object"
                    )
            problems += [f"collector {name!r}: expect {d}" for d in _differences(spec.get("expect", {}), facts)]
            if problems:
                out.warnings.append("collector-mismatch")
                logger.warning("collector %s: %d difference(s) from what was declared or expected", name, len(problems))
                for problem in problems[:MAX_LOGGED_DIFFS]:
                    logger.warning("%s", problem)
                if len(problems) > MAX_LOGGED_DIFFS:
                    logger.warning("collector %s: and %d more", name, len(problems) - MAX_LOGGED_DIFFS)
        if problems and checks and refuse:  # an unchecked collector that fails only warns
            out.refusals += [Refusal(problem) for problem in problems[:MAX_DIFFS]]
    return out
