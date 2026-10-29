"""UC-04: two environments measured by separate `bx bench` calls, then compared.

The two environments are emulated in process: an environment variable stands for
what differs between two venvs, and a context collector (or a setup file) reads it,
as it would read a package version. The session flags are `bx bench`'s --run-key,
--label, --round and --slot; the comparison is `bx compare --run`.
"""

import json
import sys
import textwrap

import pytest

pytest.importorskip("pyperf")

from benchx import cli

MODULE = "def bench_a():\n    sum(range(100))\n"
FAST = ["--processes", "2", "--values", "2", "--loops", "20", "--no-project"]
COLLECT = textwrap.dedent('''
    import json, os
    print(json.dumps({"summary": {"dep": os.environ.get("FAKE_DEP", "unset")}}))
''')


@pytest.fixture
def tree(tmp, monkeypatch):
    path = tmp / "tree"
    path.mkdir()
    (path / "mod.py").write_text(MODULE)
    monkeypatch.chdir(path)
    return path


def write_config(tree, collector=None):
    config = {"schema_version": "benchx/config/0.1.0", "adapter": {"name": "pyperf"}}
    if collector is not None:
        config["collectors"] = {"deps": {"command": [sys.executable, "-c", COLLECT], **collector}}
    (tree / "benchx.conf.json").write_text(json.dumps(config))


def call(side, round_, slot, *, dep=None, setup=None, monkeypatch, run_key="S"):
    """One `bx bench` call, as one side of the session, in the environment `dep` or `setup` stand for."""
    if dep is not None:
        monkeypatch.setenv("FAKE_DEP", dep)
    if setup is not None:
        monkeypatch.setenv("BENCHX_SETUP_FILE", str(setup))
    argv = ["bench", "mod.py", "bench_a", "--run-key", run_key, "--label", f"env={side}",
            "--round", str(round_), "--slot", str(slot), *FAST]
    assert cli.main(argv) == cli.EXIT_OK


def loop(sides, monkeypatch, rounds=3):
    """The caller's loop: alternate the sides round by round, keeping the slot counter."""
    slot = 0
    for round_ in range(rounds):
        for side, environment in sides.items():
            call(side, round_, slot, monkeypatch=monkeypatch, **environment)
            slot += 1


def compare(capsys, baseline="a", run_key="S"):
    code = cli.main(["compare", "--run", run_key, "--profile", "environments", "--label", "env",
                     "--baseline", baseline])
    return code, capsys.readouterr().out


def test_a_collector_tells_the_sides_apart(tree, monkeypatch, capsys):
    """The sides differ in what the collector reads, so the comparison goes ahead and says what differs."""
    write_config(tree, {})
    loop({"a": {"dep": "1.0"}, "b": {"dep": "2.0"}}, monkeypatch)
    capsys.readouterr()
    code, out = compare(capsys)
    assert code == cli.EXIT_OK
    assert "failed invariants" not in out
    assert "differs:" in out and "coordinates.environment.system.deps" in out
    assert "bench_a" in out


def test_a_setup_file_per_environment_tells_the_sides_apart(tree, tmp, monkeypatch, capsys):
    write_config(tree)
    files = {}
    for side, version in (("a", "1.0"), ("b", "2.0")):
        files[side] = tmp / f"setup-{side}.json"
        files[side].write_text(json.dumps({"dep": version}))
    loop({"a": {"setup": files["a"]}, "b": {"setup": files["b"]}}, monkeypatch)
    capsys.readouterr()
    code, out = compare(capsys)
    assert code == cli.EXIT_OK
    assert "failed invariants" not in out and "coordinates.environment.system.setup" in out


def test_identical_sides_are_refused_with_advice(tree, monkeypatch, capsys):
    write_config(tree, {})
    call("a", 0, 0, dep="1.0", monkeypatch=monkeypatch)
    call("b", 0, 1, dep="1.0", monkeypatch=monkeypatch)
    capsys.readouterr()
    _, out = compare(capsys)
    assert "sides-differ" in out and "collector" in out and "setup file" in out


def test_sides_that_do_not_alternate_are_refused_with_advice(tree, monkeypatch, capsys):
    write_config(tree, {})
    for slot, (side, round_, dep) in enumerate([("a", 0, "1.0"), ("a", 1, "1.0"), ("b", 0, "2.0"), ("b", 1, "2.0")]):
        call(side, round_, slot, dep=dep, monkeypatch=monkeypatch)
    capsys.readouterr()
    _, out = compare(capsys)
    assert "alternating-slots" in out and "same order every round" in out


def test_a_label_missing_on_a_call_is_refused_with_advice(tree, monkeypatch, capsys):
    write_config(tree, {})
    call("a", 0, 0, dep="1.0", monkeypatch=monkeypatch)
    capsys.readouterr()
    cli.main(["compare", "--run", "S", "--profile", "environments", "--label", "env_name", "--baseline", "a"])
    out = capsys.readouterr()
    assert "provenance.labels.env_name" in out.out + out.err
    assert "--label NAME=VALUE" in out.out + out.err


def test_a_failed_check_is_shown_in_the_comparison_and_changes_nothing_else(tree, monkeypatch, capsys):
    """expect, with on_fail: warn, flags the side that disagrees; the run goes ahead, the verdict is unchanged."""
    write_config(tree, {"expect": {"summary": {"dep": "1.0"}}, "on_fail": "warn"})
    loop({"a": {"dep": "1.0"}, "b": {"dep": "2.0"}}, monkeypatch)
    capsys.readouterr()
    code, out = compare(capsys)
    assert code == cli.EXIT_OK
    caveats = next(line for line in out.splitlines() if line.strip().startswith("caveats:"))
    assert "contender: collector-mismatch" in caveats  # beside any dirty-tree caveats the fixture tree earns
    assert "baseline: collector-mismatch" not in out
    assert "failed invariants" not in out and "bench_a" in out
