"""The precision config file, `.benchx/config.json` (PYPERF_PLAN.md step 4).

    {"adapters": {"pyperf": {"precision": {...}}}}

The precision block is spelled as in the work order (schemas/work-order
`precision`) and validated against it. It may be partial: a repetition level,
or calibration or warmup, that it leaves out takes the adapter's default.

Precedence: flags, then the file, then the adapter defaults. `resolve` returns
the complete precision, which goes into the order, so the order and its hash
hold the values that applied.
"""

import copy
import json
from pathlib import Path

from . import adapters, core
from .core import loader
from . import target as target_mod

CONFIG_FILE = Path(".benchx") / "config.json"


class ConfigError(Exception):
    """The config file is unusable; the message names the file and the problem."""


def template(adapter_name="pyperf") -> dict:
    """A config file that states the adapter's defaults, for the user to edit."""
    return {"adapters": {adapter_name: {"precision": adapters.get(adapter_name).default_precision()}}}


def find(start="."):
    return target_mod.find_upward(CONFIG_FILE, start)


def load(adapter_name, start="."):
    """The precision block for one adapter from the nearest config file, as
    `(block, path)`; `({}, None)` when there is no file or no block."""
    path = find(start)
    if path is None:
        return {}, None
    document, errors = loader.read(path)
    if document is None:
        raise ConfigError(f"{path}: " + "; ".join(str(error) for error in errors))
    if set(document) - {"adapters"}:
        raise ConfigError(f'{path}: only "adapters" is allowed at the top level')
    adapter_blocks = document.get("adapters", {})
    if not isinstance(adapter_blocks, dict):
        raise ConfigError(f'{path}: "adapters" must be an object')
    for name in adapter_blocks:
        if adapters.get(name) is None:
            raise ConfigError(f"{path}: no adapter named {name!r}")
    block = adapter_blocks.get(adapter_name, {})
    if not isinstance(block, dict) or set(block) - {"precision"}:
        raise ConfigError(f'{path}: adapters.{adapter_name} may hold only "precision"')
    precision = block.get("precision", {})
    errors = core.check_precision({"repetitions": 1, **precision} if isinstance(precision, dict) else precision)
    if errors:
        problems = "; ".join(str(error) for error in errors)
        raise ConfigError(f"{path}: adapters.{adapter_name}.precision: {problems}")
    return precision, path


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


def resolve(adapter_name, flags=None, start="."):
    """The complete precision, and the config file it read (or None)."""
    adapter = adapters.get(adapter_name)
    file_block, path = load(adapter_name, start)
    precision = merge(merge(adapter.default_precision(), file_block), flags or {})
    errors = core.check_precision(precision)
    if errors:
        raise ConfigError("precision: " + "; ".join(str(error) for error in errors))
    return precision, path


def write_template(directory=".", adapter_name="pyperf") -> Path:
    """Write `.benchx/config.json` under `directory`; never overwrites."""
    path = Path(directory) / CONFIG_FILE
    if path.exists():
        raise ConfigError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template(adapter_name), indent=1) + "\n")
    return path
