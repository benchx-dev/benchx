"""Target descriptions: what a target provider hands the workbench.

system-decomposition.md §3.4 defines the contract; schemas/target-description
and schemas/target-provider-request are its documents. A description arrives
three ways, and `resolve` handles each:

- `@CONFIG[=SOURCE_REF]`: the project's provider command, named in
  `benchx.conf.json`, asked to `prepare` (it may build) or `describe` (it only
  reports).
- `BUILD_DIR[:SOURCE_DIR]` with a sidecar `.benchx-target.json` in the build
  directory, written by whatever built it.
- `BUILD_DIR[:SOURCE_DIR]` alone: no description; the workbench identifies the
  target itself, as before.

The text a description carries (how_built, activation, shell, provider) is
declared provenance. It is recorded and never executed or checked.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

from . import core, log
from .core import loader
from .errors import BenchxError

logger = logging.getLogger(__name__)

SIDECAR = ".benchx-target.json"
TEXT_FIELDS = ("how_built", "activation", "shell", "provider")
MAX_FACTS_CHARS = 65536  # how_built as an object, serialized; as text it is limited to 8192


class TargetError(BenchxError):
    """The target cannot be described; the message says why."""


def text(description: dict | None) -> dict:
    """The provenance text of a description, with every empty value dropped.

    Absent, null, empty, and whitespace-only all mean not declared, so a
    resolved order is the same whichever spelling the user chose.
    """
    out = {}
    for field in TEXT_FIELDS:
        value = (description or {}).get(field)
        if isinstance(value, str):
            value = value if value.strip() else None
        if value:
            out[field] = value
    return out


def call_provider(command, cwd, operation, source_ref, configuration) -> dict:
    """One request to the provider: JSON on stdin, the description on stdout.

    stderr is the provider's to use (progress, build output) and goes straight
    to the user's terminal. A nonzero exit is the failure report.
    """
    request = {"schema_version": core.catalog.TARGET_PROVIDER_REQUEST, "operation": operation, "source_ref": source_ref}
    if configuration:
        request["configuration"] = configuration
    errors = core.check(request, "target-provider-request")
    if errors:
        raise TargetError("; ".join(str(error) for error in errors))
    label = f"@{configuration}" if configuration else source_ref
    # A header for the build output that streams through the terminal next
    logger.info("provider: %s %s (%s)", operation, label, source_ref)
    logger.debug("provider command: %s (in %s); request: %s", log.command(command), cwd, json.dumps(request))
    started = time.monotonic()
    try:
        done = subprocess.run(command, input=json.dumps(request), text=True, cwd=cwd, stdout=subprocess.PIPE)
    except OSError as e:
        raise TargetError(f"cannot run the provider {command[0]!r}: {e}") from None
    status, stdout = done.returncode, done.stdout
    logger.info("provider: %s %s: exit %s, %.1f s", operation, label, status, time.monotonic() - started)
    # The answer, before the exit status is judged: a failing provider's stdout is lost otherwise. The
    # last 40 lines at DEBUG, WARNING when it failed; the refusal below carries the problems, once.
    log.harness_output(logger, "provider", "stdout", stdout, failed=status != 0)
    if status != 0:
        raise TargetError(f"the provider failed to {operation} {label} (exit {status})")
    return _validated(stdout, f"the provider's answer for {label}")


def _validated(raw: str, where: str) -> dict:
    description, errors = loader.parse(raw)
    if description is not None:
        errors = core.check(description, "target-description")
    if errors:
        problems = "; ".join(str(error) for error in errors)
        raise TargetError(f"{where} is not a target description: {problems}")
    facts = description.get("how_built")
    if isinstance(facts, dict) and len(json.dumps(facts)) > MAX_FACTS_CHARS:
        raise TargetError(f"{where}: how_built facts are over {MAX_FACTS_CHARS} characters as JSON")
    return description


def sidecar(build_dir) -> dict | None:
    """The sidecar description in a build directory, if its builder wrote one."""
    path = Path(build_dir) / SIDECAR
    if not path.is_file():
        logger.debug("no sidecar %s", path)
        return None
    logger.debug("sidecar %s found", path)
    description = _validated(path.read_text(encoding="utf-8"), str(path))
    target = description["target"]
    if target["kind"] != "build" or Path(target["path"]).resolve() != Path(build_dir).resolve():
        # A copied or moved build directory must not borrow another's description.
        raise TargetError(f"{path} describes {target.get('path', target['kind'])}, not {Path(build_dir).resolve()}")
    return description


def resolve(spec: str, *, operation="prepare", provider=None) -> dict | None:
    """The description for one target spec, or None when there is none.

    `@CONFIG[=SOURCE_REF]` asks the provider, a (command, directory) pair
    (`Config.provider`), to `prepare` by default; a build directory yields its
    sidecar. SOURCE_REF defaults to WORKSPACE.
    """
    if spec.startswith("@"):
        configuration, _, source_ref = spec[1:].partition("=")
        if not configuration:
            raise TargetError(f"empty configuration name in target {spec!r}")
        if provider is None:
            raise TargetError(f"{spec} needs a provider")
        command, directory = provider
        if not source_ref:
            logger.debug("%s has no SOURCE_REF: WORKSPACE is used", spec)
        return call_provider(command, directory, operation, source_ref or "WORKSPACE", configuration)
    build = spec.partition(":")[0]
    return sidecar(build) if build else None
