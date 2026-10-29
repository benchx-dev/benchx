"""The project configuration file, `benchx.conf.json`, in the project root.

    schemas/config/0.1.0

One file says how a project is measured: the harness and its suite, quantities
and precision; the target provider; the project's name and source; environment
variables and timeouts; where the store and the result files go; and the
comparator's policy. The directory holding it is the project root, found by
walking up from the working directory. With no file the root is the working
directory and every value comes from the command line or a built-in default.

Relative paths in the file, and the defaults for the store and the results, are
relative to the root, never to the working directory.

Precedence, for every value: the command line, then this file, then a built-in
default; the one exception is the store, which `$BENCHX_HOME` relocates over the file. A value that is required and has none of the three is a hard failure
that names the flag and the key (`Config.pick`).
"""

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import adapters, core
from .core import loader

CONFIG_FILE = "benchx.conf.json"
# Files of the format this one replaced. They are not read; finding one is an error, so
# no setting silently stops applying.
LEGACY_FILES = (Path(".benchx") / "provider.json", Path(".benchx") / "config.json")
HOME_ENV = "BENCHX_HOME"
DEFAULT_STORE = "store.parquet"
DEFAULT_RESULTS = "results"
REQUIRED = object()


class ConfigError(Exception):
    """The configuration is unusable, or a required value is missing; the message says why."""


def _find_upward(relative, start) -> Path | None:
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        path = directory / relative
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class Config:
    root: Path
    path: Path | None  # None: no file was found, and root is the working directory
    data: dict

    @property
    def where(self) -> str:
        return str(self.path) if self.path else f"{CONFIG_FILE} (none found from {self.root} upward)"

    def section(self, name) -> dict:
        return self.data.get(name, {})

    @property
    def adapter(self) -> dict:
        return self.section("adapter")

    def pick(self, flag_value, file_value, default=REQUIRED, *, flag, key):
        """The command-line value, else the file's, else `default`; ConfigError when
        none is given and the value is required."""
        for value in (flag_value, file_value):
            if value is not None:
                return value
        if default is REQUIRED:
            raise ConfigError(f"{flag} is required: give it, or set {key} in {self.where}")
        return default

    def _path(self, value) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.root / path

    @property
    def store_path(self) -> Path:
        """The store: `$BENCHX_HOME/store.parquet` when that is set (it wins over the
        file, as it relocates the store for tests and CI), else `paths.store`."""
        if home := os.environ.get(HOME_ENV):
            return Path(home).expanduser() / DEFAULT_STORE
        return self._path(self.section("paths").get("store", DEFAULT_STORE))

    @property
    def results_dir(self) -> Path:
        return self._path(self.section("paths").get("results", DEFAULT_RESULTS))

    @property
    def collectors(self) -> list[dict]:
        """The context collectors, as a work order lists them. `{root}` in a command
        argument is the project root, because a collector runs in the harness's
        working directory, not here."""
        out = []
        for name, spec in self.section("collectors").items():
            command = [argument.replace("{root}", str(self.root)) for argument in spec["command"]]
            out.append({"name": name, **spec, "command": command})
        return out

    @property
    def provider(self) -> tuple[list[str], Path] | None:
        """The provider command, and the directory it runs in (the root)."""
        command = self.section("provider").get("command")
        return (list(command), self.root) if command else None

    def require_provider(self, spec: str) -> tuple[list[str], Path]:
        found = self.provider
        if found is None:
            raise ConfigError(f'{spec} needs "provider" in {self.where}')
        return found


def load(start=".") -> Config:
    """The nearest `benchx.conf.json` walking up from `start`, validated; or an empty
    configuration rooted at `start` when there is none."""
    start = Path(start).resolve()
    path = _find_upward(CONFIG_FILE, start)
    if path is None:
        for legacy in LEGACY_FILES:
            if old := _find_upward(legacy, start):
                raise ConfigError(f"{old} is the old configuration format, which is no longer read: "
                                  f"move its settings into {CONFIG_FILE} (schemas/config/0.1.0)")
        return Config(root=start, path=None, data={})
    data, errors = loader.read(path)
    if data is None:
        raise ConfigError(f"{path}: " + "; ".join(str(error) for error in errors))
    if errors := core.check(data, "config"):
        raise ConfigError(f"{path}: " + "; ".join(str(error) for error in errors))
    _check_adapter(path, data.get("adapter"))
    return Config(root=path.parent, path=path, data=data)


def _check_adapter(path, block):
    if block is None:
        return
    adapter = adapters.get(block["name"])
    if adapter is None:
        raise ConfigError(f"{path}: adapter.name: no adapter named {block['name']!r} ({', '.join(sorted(adapters.ADAPTERS))})")
    unknown = sorted(set(block.get("quantities", [])) - set(adapter.QUANTITIES))
    if unknown:
        raise ConfigError(f"{path}: adapter.quantities: {block['name']} does not report {', '.join(unknown)}")
    precision = block.get("precision", {})
    if errors := core.check_precision({"repetitions": 1, **precision}):
        raise ConfigError(f"{path}: adapter.precision: " + "; ".join(str(error) for error in errors))


def merge(base: dict, over: dict) -> dict:
    """`over` on top of `base`: repetition levels are merged by unit, and
    calibration and warmup replace as wholes."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        if key == "repetitions" and isinstance(value, dict) and isinstance(merged.get(key), dict):
            levels = {lv["unit"]: lv for lv in merged[key]["levels"]}
            levels.update((lv["unit"], lv) for lv in value["levels"])
            merged[key] = {"mode": "fixed", "levels": list(levels.values())}
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parse_quantities(text: str) -> list[str]:
    """`--quantity wall-time,cpu-time` as a list; ConfigError on an empty or repeated name."""
    names = [name.strip() for name in text.split(",")]
    if not all(names):
        raise ConfigError(f"--quantity wants comma-separated names, not {text!r}")
    if len(set(names)) != len(names):
        raise ConfigError(f"--quantity names a quantity twice: {text!r}")
    return names


def template(adapter_name="pyperf") -> dict:
    """A configuration that states the harness's defaults, for the user to edit."""
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise ConfigError(f"no adapter named {adapter_name!r} ({', '.join(sorted(adapters.ADAPTERS))})")
    return {"schema_version": "benchx/config/0.1.0",
            "adapter": {"name": adapter_name, "quantities": ["wall-time"], "precision": adapter.default_precision()}}


def write_template(directory=".", adapter_name="pyperf") -> Path:
    """Write `benchx.conf.json` under `directory`; never overwrites."""
    path = Path(directory) / CONFIG_FILE
    if path.exists():
        raise ConfigError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template(adapter_name), indent=1) + "\n")
    return path
