"""The precision config file (PYPERF_PLAN.md step 4)."""

import json

import pytest

from benchx import config


def levels(precision):
    return {lv["unit"]: lv["n"] for lv in precision["repetitions"]["levels"]}


def write_config(directory, precision):
    path = directory / ".benchx" / "config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"adapters": {"pyperf": {"precision": precision}}}))
    return path


@pytest.fixture
def tree(tmp, monkeypatch):
    path = tmp / "tree"
    path.mkdir()
    (path / "mod.py").write_text("def bench_a():\n    pass\n")
    monkeypatch.chdir(path)
    return path


def test_defaults_are_pyperfs_when_there_is_no_file(tree):
    precision, path = config.resolve("pyperf", start=tree)
    assert path is None
    assert levels(precision) == {"process": 20, "value": 3}
    assert precision["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.1}


def test_file_overrides_defaults_and_flags_override_the_file(tree):
    write_config(tree, {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 7}]},
                        "calibration": {"mode": "fixed", "n_iterations": 30}})
    precision, path = config.resolve("pyperf", start=tree)
    assert path == tree / ".benchx" / "config.json"
    assert levels(precision) == {"process": 7, "value": 3}  # the file's level, pyperf's for the other
    assert precision["calibration"] == {"mode": "fixed", "n_iterations": 30}

    flags = {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 4}]},
             "calibration": {"mode": "adaptive", "minimum_sample_seconds": 0.5}}
    precision, _ = config.resolve("pyperf", flags, start=tree)
    assert levels(precision) == {"process": 4, "value": 3}
    assert precision["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.5}


def test_config_found_by_walking_up(tree):
    write_config(tree, {"warmup": {"mode": "none"}})
    deep = tree / "a" / "b"
    deep.mkdir(parents=True)
    precision, _ = config.resolve("pyperf", start=deep)
    assert precision["warmup"] == {"mode": "none"}


@pytest.mark.parametrize("content, message", [
    ({"adapters": {"pyperf": {"precision": {"calibration": {"mode": "fixed"}}}}}, "n_iterations"),
    ({"adapters": {"pyperf": {"precision": {"warmups": 3}}}}, "warmups"),
    ({"adapters": {"nonesuch": {}}}, "no adapter named"),
    ({"adapters": {"pyperf": {"speed": "fast"}}}, "precision"),
    ({"speed": "fast"}, "top level"),
])
def test_bad_config_is_rejected(tree, content, message):
    (tree / ".benchx").mkdir()
    (tree / ".benchx" / "config.json").write_text(json.dumps(content))
    with pytest.raises(config.ConfigError, match=message):
        config.resolve("pyperf", start=tree)


def test_malformed_config_is_rejected(tree):
    (tree / ".benchx").mkdir()
    (tree / ".benchx" / "config.json").write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.resolve("pyperf", start=tree)


def test_template_states_pyperfs_defaults_and_is_valid(tree):
    path = config.write_template(tree)
    precision, found = config.resolve("pyperf", start=tree)
    assert found == path and levels(precision) == {"process": 20, "value": 3}
    with pytest.raises(config.ConfigError, match="already exists"):
        config.write_template(tree)
