"""Context collectors and the check of a target's declared facts against them (benchx.collectors)."""

import json
import sys
import textwrap

import pytest

from benchx import collectors, compare, config, core, runner
from conftest import git, warnings_of


def spec(name="facts", script='print(\'{"a": 1}\')', **extra):
    return {"name": name, "command": [sys.executable, "-c", script], **extra}


def run_all(specs, tmp, declared=None, taken=()):
    (tmp / "artifacts").mkdir(exist_ok=True)
    return collectors.run_all(specs, declared=declared, cwd=tmp, env={}, artifacts_dir=tmp / "artifacts", taken=taken)


def messages(collected):
    return [str(refusal) for refusal in collected.refusals]


def test_facts_are_recorded_in_full_and_in_summary(tmp):
    script = 'import json; print(json.dumps({"big": [1, 2], "summary": {"version": "9"}}))'
    collected = run_all([spec(script=script)], tmp)
    assert collected.warnings == [] and collected.refusals == []
    assert collected.system["facts"]["version"] == "9"
    assert collected.system["facts"]["sha256"] == core.sha256_hex(core.canonical({"big": [1, 2], "summary": {"version": "9"}}))
    ((kind, media_type, path),) = collected.artifacts
    assert (kind, media_type) == ("collector-output", "application/json")
    assert json.loads(path.read_text())["big"] == [1, 2]


@pytest.mark.parametrize("script", ["import sys; sys.exit(3)", "print('not json')", "print('[1, 2]')",
                                    "import time; time.sleep(5)"])
def test_a_failed_collector_adds_no_facts_and_only_warns(tmp, script):
    collected = run_all([spec(script=script, timeout_s=1)], tmp)
    assert collected.warnings == ["collector-failed"] and collected.system == {} and collected.refusals == []


def test_a_missing_command_is_a_failure(tmp):
    collected = run_all([{"name": "x", "command": ["/no/such/collector"]}], tmp)
    assert collected.warnings == ["collector-failed"]


def test_declared_facts_must_be_observed_exactly_extras_are_fine(tmp):
    script = 'import json; print(json.dumps({"a": {"b": 1, "c": [1, 2]}, "extra": 0}))'
    ok = run_all([spec(script=script, verify="how_built")], tmp, declared={"a": {"b": 1, "c": [1, 2]}})
    assert ok.refusals == [] and ok.warnings == []
    bad = run_all([spec(script=script, verify="how_built")], tmp, declared={"a": {"b": 2, "c": [1, 2]}, "gone": True})
    assert any("how_built /a/b: declared 2, observed 1" in m for m in messages(bad))
    assert any("how_built /gone: missing" in m for m in messages(bad))
    assert bad.warnings == ["collector-mismatch"]


def test_verify_needs_declared_facts(tmp):
    collected = run_all([spec(verify="how_built")], tmp, declared="spin build")
    assert "not an object" in messages(collected)[0]


def test_expect_compares_by_value(tmp):
    assert run_all([spec(expect={"a": 1})], tmp).refusals == []
    collected = run_all([spec(expect={"a": 2})], tmp)
    assert "expect /a: declared 2, observed 1" in messages(collected)[0]


def test_warn_runs_on_and_marks_the_results(tmp):
    collected = run_all([spec(expect={"a": 2}, on_fail="warn")], tmp)
    assert collected.refusals == [] and collected.warnings == ["collector-mismatch"]
    assert "facts" in collected.system  # the facts are still recorded


def test_a_collector_that_cannot_run_cannot_verify(tmp):
    broken = spec(script="import sys; sys.exit(1)", verify="how_built")
    assert "failed" in messages(run_all([broken], tmp, declared={"a": 1}))[0]
    assert run_all([{**broken, "on_fail": "warn"}], tmp, declared={"a": 1}).refusals == []


def test_a_name_may_not_shadow_other_system_facts(tmp):
    assert "already in environment.system" in messages(run_all([spec(name="os")], tmp, taken={"os": 1}))[0]
    assert "already in environment.system" in messages(run_all([spec(), spec()], tmp))[0]


def test_comparison_shows_structured_how_built_as_json_text():
    document = {"coordinates": {"environment": {}}, "provenance": {"info": {"how_built": {"b": 1, "a": {"c": 2}}}}}
    assert compare._declared(document)["how_built"] == '{"a":{"c":2},"b":1}'


# --- configuration and the runner ---------------------------------------------------

def write_config(directory, **collectors):
    path = directory / config.CONFIG_FILE
    path.write_text(json.dumps({"schema_version": "benchx/config/0.1.0", "collectors": collectors}))
    return path


def test_the_root_is_substituted_in_a_collector_command(tmp):
    write_config(tmp, numpy={"command": ["python", "{root}/facts.py"], "verify": "how_built"})
    (entry,) = config.load(tmp).collectors
    assert entry == {"name": "numpy", "command": ["python", f"{tmp.resolve()}/facts.py"], "verify": "how_built"}


@pytest.mark.parametrize("bad", [{"command": []}, {"command": ["x"], "on_fail": "ignore"},
                                 {"command": ["x"], "verify": "activation"}, {"command": ["x"], "speed": 1}])
def test_a_bad_collector_is_refused(tmp, bad):
    write_config(tmp, c=bad)
    with pytest.raises(config.ConfigError):
        config.load(tmp)


pytest.importorskip("pyperf")
from benchx import cli  # noqa: E402

SUITE = "def bench_a():\n    sum(range(100))\n"
FAST = ["--processes", "2", "--values", "2", "--loops", "20"]


@pytest.fixture
def project(tmp, monkeypatch):
    """A subject checkout, a suite and a provider that declares how_built = {"k": 1}."""
    subject = tmp / "subject"
    subject.mkdir()
    git(subject, "init", "-q")
    git(subject, "remote", "add", "origin", "https://example.org/subject.git")
    (subject / "lib.py").write_text("X = 1\n")
    git(subject, "add", "-A")
    git(subject, "commit", "-qm", "one")
    root = tmp / "proj"
    root.mkdir()
    (root / "bench_a.py").write_text(SUITE)
    (root / "provider.py").write_text(textwrap.dedent(f'''
        import json, sys
        sys.stdin.read()
        print(json.dumps({{"schema_version": "benchx/target-description/0.1.0",
                          "target": {{"kind": "build", "path": "{subject}", "source_dir": "{subject}",
                                     "source": {{"uri": "https://example.org/subject.git", "type": "git"}}}},
                          "how_built": {{"k": 1}}}}))
    '''))
    (root / "facts.py").write_text("import json, os; print(json.dumps({'k': int(os.environ.get('K', 1)), 'summary': {'s': 1}}))\n")
    monkeypatch.chdir(root)
    return root


def configure(root, **collector):
    (root / config.CONFIG_FILE).write_text(json.dumps({
        "schema_version": "benchx/config/0.1.0",
        "provider": {"command": [sys.executable, "provider.py"]},
        "adapter": {"name": "pyperf", "suite": str(root / "bench_a.py")},
        "collectors": {"facts": {"command": [sys.executable, "{root}/facts.py"], "verify": "how_built", **collector}}}))


def result_of(run_key):
    from pathlib import Path
    path = next(p for p in sorted(Path("results", run_key).glob("*_wall-time.json")))
    return json.loads(path.read_text())


def test_bench_runs_the_collector_and_records_its_facts(project):
    configure(project)
    assert cli.main(["bench", "@x", *FAST, "--run-key", "c1"]) == cli.EXIT_OK
    document = result_of("c1")
    assert core.check(document, "measurement-result") == []
    assert document["coordinates"]["environment"]["system"]["facts"]["s"] == 1 and "sha256" in document["coordinates"]["environment"]["system"]["facts"]
    assert document["provenance"]["info"]["how_built"] == {"k": 1}
    assert "collector-output" in {a["kind"] for a in document["provenance"]["artifacts"]}
    assert warnings_of(document) == []


def test_a_mismatch_refuses_the_order_before_any_case(project, capsys, monkeypatch):
    configure(project)
    monkeypatch.setenv("K", "2")
    assert cli.main(["bench", "@x", *FAST, "--run-key", "c2"]) == cli.EXIT_REFUSED
    assert "how_built /k: declared 1, observed 2" in capsys.readouterr().err
    assert not list(project.glob("results/c2/*_wall-time.json"))


def test_a_warn_mismatch_runs_and_marks_every_result(project, monkeypatch):
    configure(project, on_fail="warn")
    monkeypatch.setenv("K", "2")
    assert cli.main(["bench", "@x", *FAST, "--run-key", "c3"]) == cli.EXIT_OK
    assert warnings_of(result_of("c3")) == ["collector-mismatch"]


def test_bx_run_refuses_an_order_with_collectors_unless_allowed(project, tmp, capsys):
    configure(project)
    assert cli.main(["bench", "@x", *FAST, "--run-key", "c4", "--no-ingest"]) == cli.EXIT_OK
    order = project / "results" / "c4" / "orders" / "order.json"
    assert cli.main(["run", str(order), "--out", str(tmp / "o1")]) == cli.EXIT_REFUSED
    assert "--allow-collectors" in capsys.readouterr().err
    with pytest.raises(runner.Refused):
        runner.run(order, tmp / "o2")
    assert cli.main(["run", str(order), "--out", str(tmp / "o3"), "--allow-collectors", "--no-ingest"]) == cli.EXIT_OK
