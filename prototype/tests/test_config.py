"""benchx.conf.json: where it is found, what it holds, and how a value is picked (benchx.config)."""

import json

import pytest

from benchx import cli, config, harnesses


def levels(precision):
    return {lv["unit"]: lv["n"] for lv in precision["repetitions"]["levels"]}


def write_config(directory, **content):
    path = directory / config.CONFIG_FILE
    path.write_text(json.dumps({"schema_version": "benchx/config/0.1.0", **content}))
    return path


@pytest.fixture
def tree(tmp):
    path = tmp / "tree"
    path.mkdir()
    return path


# --- where it is found -------------------------------------------------------------

def test_without_a_file_the_working_directory_is_the_root(tree):
    cfg = config.load(tree)
    assert (cfg.path, cfg.root, cfg.data) == (None, tree.resolve(), {})
    assert cfg.store_path == tree.resolve() / "store.parquet"
    assert cfg.results_dir == tree.resolve() / "results"
    assert cfg.provider is None


def test_the_file_is_found_walking_up_and_its_directory_is_the_root(tree):
    path = write_config(tree, project="demo")
    deep = tree / "a" / "b"
    deep.mkdir(parents=True)
    cfg = config.load(deep)
    assert (cfg.path, cfg.root) == (path, tree.resolve())
    assert cfg.store_path == tree.resolve() / "store.parquet"  # the root's, not the working directory's


def test_paths_are_relative_to_the_root_unless_absolute(tree, tmp):
    write_config(tree, paths={"store": "data/s.parquet", "results": str(tmp / "elsewhere")})
    (tree / "sub").mkdir()
    cfg = config.load(tree / "sub")
    assert cfg.store_path == tree.resolve() / "data" / "s.parquet"
    assert cfg.results_dir == tmp / "elsewhere"


def test_the_provider_runs_in_the_root(tree):
    write_config(tree, provider={"command": ["./provider.sh"]})
    assert config.load(tree).provider == (["./provider.sh"], tree.resolve())


# --- what is refused ---------------------------------------------------------------

@pytest.mark.parametrize("content, message", [
    ({"adapter": {"name": "nonesuch"}}, "no adapter named"),
    ({"adapter": {"name": "pyperf", "quantities": ["cpu-time"]}}, "does not report cpu-time"),
    ({"adapter": {"name": "pyperf", "precision": {"calibration": {"mode": "fixed"}}}}, "n_iterations"),
    ({"adapter": {"name": "pyperf", "precision": {"warmups": 3}}}, "warmups"),
    ({"adapter": {"suite": "s"}}, "name"),
    ({"provider": {"command": []}}, "command"),
    ({"speed": "fast"}, "speed"),
])
def test_a_bad_file_is_refused(tree, content, message):
    write_config(tree, **content)
    with pytest.raises(config.ConfigError, match=message):
        config.load(tree)


def test_a_malformed_file_is_refused(tree):
    (tree / config.CONFIG_FILE).write_text("{not json")
    with pytest.raises(config.ConfigError):
        config.load(tree)


@pytest.mark.parametrize("legacy", [".benchx/provider.json", ".benchx/config.json"])
def test_the_old_files_are_not_read_and_not_ignored(tree, legacy):
    (tree / ".benchx").mkdir()
    (tree / legacy).write_text("{}")
    with pytest.raises(config.ConfigError, match="old configuration format"):
        config.load(tree)
    write_config(tree)  # once the new file exists, the old one is just a file
    assert config.load(tree).path is not None


# --- picking a value ---------------------------------------------------------------

def test_the_command_line_wins_then_the_file_then_the_default(tree):
    cfg = config.load(tree)
    assert cfg.pick("flag", "file", "default", flag="--x", key="x") == "flag"
    assert cfg.pick(None, "file", "default", flag="--x", key="x") == "file"
    assert cfg.pick(None, None, "default", flag="--x", key="x") == "default"
    assert cfg.pick(None, None, None, flag="--x", key="x") is None  # None is a default too
    assert cfg.pick(0, 5, 9, flag="--x", key="x") == 0  # a falsy flag still counts as given


def test_a_missing_required_value_names_the_flag_and_the_key(tree):
    with pytest.raises(config.ConfigError, match=r"--suite is required: give it, or set adapter\.suite in .*none found"):
        config.load(tree).pick(None, None, flag="--suite", key="adapter.suite")
    write_config(tree)
    with pytest.raises(config.ConfigError, match=r"--suite is required: give it, or set adapter\.suite in .*benchx\.conf\.json"):
        config.load(tree).pick(None, None, flag="--suite", key="adapter.suite")


def test_a_provider_is_needed_only_when_a_target_asks_for_it(tree):
    cfg = config.load(tree)
    with pytest.raises(config.ConfigError, match=r'@plain needs "provider"'):
        cfg.require_provider("@plain")


@pytest.mark.parametrize("text, names", [("wall-time", ["wall-time"]), ("wall-time,cpu-time", ["wall-time", "cpu-time"]),
                                         (" wall-time , cpu-time ", ["wall-time", "cpu-time"])])
def test_quantities_are_a_comma_separated_list(text, names):
    assert config.parse_quantities(text) == names


@pytest.mark.parametrize("text", ["", "wall-time,", ",cpu-time", "wall-time,,cpu-time", "wall-time,wall-time"])
def test_empty_and_repeated_quantities_are_refused(text):
    with pytest.raises(config.ConfigError, match="--quantity"):
        config.parse_quantities(text)


# --- precision ---------------------------------------------------------------------

def test_the_file_overrides_the_defaults_and_flags_override_the_file(tree):
    file_precision = {"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 7}]},
                      "calibration": {"mode": "fixed", "n_iterations": 30}}
    pyperf = harnesses.PYPERF
    assert levels(pyperf.precision({})) == {"process": 20, "value": 3}
    precision = pyperf.precision({}, file_precision)
    assert levels(precision) == {"process": 7, "value": 3}  # the file's level, pyperf's for the other
    assert precision["calibration"] == {"mode": "fixed", "n_iterations": 30}

    flags = pyperf.flags_to_precision(processes=4, min_time=0.5)
    precision = pyperf.precision(flags, file_precision)
    assert levels(precision) == {"process": 4, "value": 3}
    assert precision["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.5}


def test_google_benchmark_precision_comes_from_the_same_layers():
    gb = harnesses.GOOGLE_BENCHMARK
    assert gb.precision({})["repetitions"] == 5
    file_precision = {"repetitions": 10, "warmup": {"mode": "time", "seconds": 0.1}}
    precision = gb.precision(gb.flags_to_precision(repetitions=3), file_precision)
    assert (precision["repetitions"], precision["warmup"]) == (3, {"mode": "time", "seconds": 0.1})


# --- bx config -----------------------------------------------------------------------

@pytest.mark.parametrize("adapter", sorted(harnesses.HARNESSES))
def test_init_writes_a_valid_file_that_states_the_defaults(tree, adapter, capsys):
    assert cli.main(["config", "init", "--dir", str(tree), "--adapter", adapter]) == cli.EXIT_OK
    cfg = config.load(tree)
    assert cfg.adapter["name"] == adapter
    assert cfg.adapter["precision"] == harnesses.get(adapter).precision({})
    assert cli.main(["config", "init", "--dir", str(tree)]) == cli.EXIT_REFUSED
    assert "already exists" in capsys.readouterr().err


def test_show_says_where_each_value_comes_from(tree, monkeypatch, capsys):
    write_config(tree, project="demo", adapter={"name": "pyperf", "suite": "mod.py"})
    monkeypatch.chdir(tree)
    assert cli.main(["config", "show"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert '"demo"  (file)' in out and '"mod.py"  (file)' in out
    assert "compare.rounds             -  (unset)" in out
    assert "timeouts.case_s            60  (default)" in out
