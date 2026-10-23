"""Target descriptions: what a target provider hands the workbench.

system-decomposition.md §3.4 defines the contract; schemas/target-description
and schemas/target-provider-request are its documents. A description arrives
three ways, and `resolve` handles each:

- `@CONFIG[=SOURCE_REF]`: the project's provider command, named in
  `.benchx/provider.json`, asked to `prepare` (it may build) or `describe`
  (it only reports).
- `BUILD_DIR[:SOURCE_DIR]` with a sidecar `.benchx-target.json` in the build
  directory, written by whatever built it.
- `BUILD_DIR[:SOURCE_DIR]` alone: no description; the workbench identifies the
  target itself, as before.

The text a description carries (how_built, activation, shell, provider) is
declared provenance. It is recorded and never executed or checked.
"""

import json
import subprocess
from pathlib import Path

from . import core
from .core import loader

SIDECAR = ".benchx-target.json"
PROVIDER_FILE = Path(".benchx") / "provider.json"
TEXT_FIELDS = ("how_built", "activation", "shell", "provider")


class TargetError(Exception):
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


def find_upward(relative, start="."):
    """The nearest file `relative` (e.g. `.benchx/config.json`) walking up from
    `start`, or None."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        path = directory / relative
        if path.is_file():
            return path
    return None


def find_provider(start="."):
    """The provider command from the nearest `.benchx/provider.json`, walking
    up from `start`. Returns (command, directory it is run in), or None."""
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        path = directory / PROVIDER_FILE
        if path.is_file():
            config, errors = loader.read(path)
            if config is None:
                raise TargetError(f"{path}: " + "; ".join(str(error) for error in errors))
            command = config.get("command")
            if not (isinstance(command, list) and command and all(isinstance(c, str) for c in command)):
                raise TargetError(f'{path} must be {{"command": ["program", "arg", ...]}}')
            return command, directory
    return None


def call_provider(command, cwd, operation, source_ref, configuration) -> dict:
    """One request to the provider: JSON on stdin, the description on stdout.

    stderr is the provider's to use (progress, build output) and goes straight
    to the user's terminal. A nonzero exit is the failure report.
    """
    request = {"schema_version": core.catalog.TARGET_PROVIDER_REQUEST, "operation": operation,
               "source_ref": source_ref}
    if configuration:
        request["configuration"] = configuration
    errors = core.check(request, "target-provider-request")
    if errors:
        raise TargetError("; ".join(str(error) for error in errors))
    label = f"@{configuration}" if configuration else source_ref
    try:
        done = subprocess.run(command, input=json.dumps(request), text=True, cwd=cwd,
                              stdout=subprocess.PIPE)
    except OSError as e:
        raise TargetError(f"cannot run the provider {command[0]!r}: {e}") from None
    if done.returncode != 0:
        raise TargetError(f"the provider failed to {operation} {label} (exit {done.returncode})")
    return _validated(done.stdout, f"the provider's answer for {label}")


def _validated(raw: str, where: str) -> dict:
    description, errors = loader.parse(raw)
    if description is not None:
        errors = core.check(description, "target-description")
    if errors:
        problems = "; ".join(str(error) for error in errors)
        raise TargetError(f"{where} is not a target description: {problems}")
    return description


def sidecar(build_dir) -> dict | None:
    """The sidecar description in a build directory, if its builder wrote one."""
    path = Path(build_dir) / SIDECAR
    if not path.is_file():
        return None
    description = _validated(path.read_text(encoding="utf-8"), str(path))
    target = description["target"]
    if target["kind"] != "build" or Path(target["path"]).resolve() != Path(build_dir).resolve():
        # A copied or moved build directory must not borrow another's description.
        raise TargetError(f"{path} describes {target.get('path', target['kind'])}, not {Path(build_dir).resolve()}")
    return description


def resolve(spec: str, *, operation="prepare", cwd=".") -> dict | None:
    """The description for one target spec, or None when there is none.

    `@CONFIG[=SOURCE_REF]` asks the provider (`prepare` by default); a build
    directory yields its sidecar. SOURCE_REF defaults to WORKSPACE.
    """
    if spec.startswith("@"):
        configuration, _, source_ref = spec[1:].partition("=")
        if not configuration:
            raise TargetError(f"empty configuration name in target {spec!r}")
        found = find_provider(cwd)
        if found is None:
            raise TargetError(f"{spec} needs a provider: no {PROVIDER_FILE} in {Path(cwd).resolve()} or above")
        command, directory = found
        return call_provider(command, directory, operation, source_ref or "WORKSPACE", configuration)
    build = spec.partition(":")[0]
    return sidecar(build) if build else None
