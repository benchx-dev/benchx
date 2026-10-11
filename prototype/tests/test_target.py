"""Target descriptions: the provider contract, the sidecar, and the declared
text that reaches orders, results, and the comparison (system-decomposition.md §3.4)."""

import json
import stat
import sys

import pytest

from benchx import compare, core, runner, session, snapshot
from benchx import target as target_mod
from benchx.store import Store
from conftest import SOURCE_URI, cases, make_build, order

PROVIDER = """#!{python}
import json, sys
request = json.load(sys.stdin)
print("progress goes to stderr", file=sys.stderr)
if request["configuration"] == "broken":
    sys.exit(7)
doc = json.load(open({descriptions!r}))[request["configuration"]]
doc["provider"] = {{"name": "fake-provider", "version": request["operation"]}}
print(json.dumps(doc))
"""


def describe(directory, source, **fields):
    return {"schema_version": "benchx/target-description/0.1.0",
            "target": {"kind": "build", "path": str(directory), "source": {"uri": SOURCE_URI},
                       "source_dir": str(source)},
            **fields}


def write_sidecar(directory, source, **fields):
    (directory / target_mod.SIDECAR).write_text(json.dumps(describe(directory, source, **fields)))


def test_text_drops_every_empty_spelling():
    assert target_mod.text({"how_built": "", "activation": None, "shell": "  \n"}) == {}
    assert target_mod.text({}) == {} and target_mod.text(None) == {}
    assert target_mod.text({"how_built": "make", "activation": "source x", "shell": "bash",
                            "provider": {"name": "p"}}) == {
        "how_built": "make", "activation": "source x", "shell": "bash", "provider": {"name": "p"}}


def test_sidecar_is_validated_and_belongs_to_its_directory(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    assert target_mod.sidecar(build) is None
    write_sidecar(build, repo["wt-head"], how_built="make")
    assert target_mod.sidecar(build)["how_built"] == "make"
    moved = tmp / "moved"
    build.rename(moved)
    with pytest.raises(target_mod.TargetError, match="describes"):
        target_mod.sidecar(moved)  # a moved directory must not keep the old description
    (moved / target_mod.SIDECAR).write_text(json.dumps({"schema_version": "benchx/target-description/0.1.0"}))
    with pytest.raises(target_mod.TargetError, match="not a target description"):
        target_mod.sidecar(moved)


def test_description_schema_rejects_what_the_contract_forbids(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    bad = describe(build, repo["wt-head"], shell="not a shell")
    [problem] = core.check(bad, "target-description")
    assert "shell" in problem.path
    relative = describe(build, repo["wt-head"])
    relative["target"]["path"] = "build/relative"
    [problem] = core.check(relative, "target-description")
    assert problem.path[-1] == "path"


@pytest.fixture
def provider(repo, tmp, monkeypatch):
    plain = make_build(tmp / "plain", repo["wt-head"], cases())
    hardened = make_build(tmp / "hardened", repo["wt-head"], cases(quiet=1.02e-3, noisy=2.06e-3), hardened=True)
    descriptions = tmp / "descriptions.json"
    descriptions.write_text(json.dumps({
        "plain": describe(plain, repo["wt-head"], how_built="cmake plain", activation=""),
        "hardened": describe(hardened, repo["wt-head"], how_built="cmake -DDEMO_HARDENED=ON\nmake",
                             activation="source env.sh", shell="bash"),
    }))
    script = tmp / "proj" / "provider.py"
    script.parent.mkdir()
    script.write_text(PROVIDER.format(python=sys.executable, descriptions=str(descriptions)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    (tmp / "proj" / ".benchx").mkdir()
    (tmp / "proj" / ".benchx" / "provider.json").write_text(json.dumps({"command": [str(script)]}))
    monkeypatch.chdir(tmp / "proj")
    return {"plain": plain, "hardened": hardened}


def test_provider_is_found_walking_up_and_answers_a_request(provider, tmp):
    (tmp / "proj" / "deeper").mkdir()
    command, directory = target_mod.find_provider(tmp / "proj" / "deeper")
    assert directory == (tmp / "proj").resolve() and len(command) == 1
    doc = target_mod.resolve("@hardened", operation="describe", cwd=tmp / "proj" / "deeper")
    assert doc["provider"] == {"name": "fake-provider", "version": "describe"}
    assert target_mod.resolve("@hardened", operation="prepare")["provider"]["version"] == "prepare"


def test_provider_failure_and_missing_provider_are_errors(provider, tmp, monkeypatch):
    with pytest.raises(target_mod.TargetError, match="failed to prepare @broken .*exit 7"):
        target_mod.resolve("@broken")
    monkeypatch.chdir(tmp)
    with pytest.raises(target_mod.TargetError, match="needs a provider"):
        target_mod.resolve("@plain")


def test_declared_text_reaches_orders_results_and_the_comparison(repo, tmp, provider):
    store = Store(tmp / "store.parquet")
    doc = session.compare_targets("@plain", "@hardened", profile="environments", suite="demo-bench", rounds=4,
                                  label=("build", "plain", "hardened"), project="demo", store=store,
                                  run_key="t1", out=tmp / "out")
    orders = [json.loads(p.read_text()) for p in sorted((tmp / "out" / "orders").glob("*.json"))]
    plain_target, hardened_target = orders[0]["target"], orders[1]["target"]
    assert plain_target["how_built"] == "cmake plain"
    assert "activation" not in plain_target and "shell" not in plain_target  # empty means not declared
    assert hardened_target["activation"] == "source env.sh" and hardened_target["shell"] == "bash"
    assert hardened_target["provider"]["name"] == "fake-provider"

    results = {d["provenance"]["labels"]["build"]: d for d in store.documents("t1")}
    info = results["hardened"]["provenance"]["info"]
    assert info["how_built"].splitlines() == ["cmake -DDEMO_HARDENED=ON", "make"]
    assert (info["activation"], info["shell"]) == ("source env.sh", "bash")
    assert info["target_provider"] == {"name": "fake-provider", "version": "prepare"}
    assert "activation" not in results["plain"]["provenance"]["info"]

    assert doc["sides"]["plain"] == {"how_built": "cmake plain", "target_provider": info["target_provider"]}
    assert doc["sides"]["hardened"]["activation"] == "source env.sh"
    assert "hardened: activation: source env.sh" in compare.render(doc)


def test_no_build_asks_the_provider_to_describe(repo, tmp, provider):
    session.compare_targets("@plain", "@hardened", profile="environments", suite="demo-bench", rounds=3,
                            label=("build", "plain", "hardened"), store=Store(tmp / "store.parquet"),
                            run_key="t2", out=tmp / "out", build=False)
    result = next(p for p in (tmp / "out").glob("*.json") if not p.name.startswith("workorder-"))
    assert json.loads(result.read_text())["provenance"]["info"]["target_provider"]["version"] == "describe"


def test_sidecar_route_needs_no_provider(repo, tmp):
    plain = make_build(tmp / "plain", repo["wt-head"], cases())
    hardened = make_build(tmp / "hardened", repo["wt-head"], cases(quiet=1.02e-3), hardened=True)
    write_sidecar(hardened, repo["wt-head"], how_built="by hand")
    doc = session.compare_targets(str(plain), str(hardened), profile="environments", suite="demo-bench",
                                  rounds=3, label=("build", "plain", "hardened"),
                                  store=Store(tmp / "store.parquet"), run_key="t3", out=tmp / "out",
                                  source_uri=SOURCE_URI)  # one side has no description to name it
    assert doc["sides"]["hardened"] == {"how_built": "by hand"} and doc["sides"]["plain"] == {}


def test_a_declared_build_is_refused_before_anything_runs(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    write_sidecar(build, repo["wt-head"], build={"type": "Release"})
    other = make_build(tmp / "o", repo["wt-head"], cases())
    with pytest.raises(session.SessionError, match="declared build is deferred"):
        session.compare_targets(str(build), str(other), profile="environments", suite="demo-bench", rounds=3,
                                label=("build", "a", "b"), store=Store(tmp / "s.parquet"), run_key="t4",
                                out=tmp / "out")
    assert not (tmp / "out").exists()


def test_runner_accepts_text_in_an_order_and_never_runs_it(repo, tmp):
    build = make_build(tmp / "b", repo["wt-head"], cases())
    marker = tmp / "ran"
    o = order(build, repo["wt-head"], "r1")
    o["target"].update(how_built=f"touch {marker}", activation=f"touch {marker}", shell="bash")
    path = tmp / "o.json"
    path.write_text(json.dumps(o))
    summary = runner.run(path, tmp / "out")
    assert summary["results"] == 2 and not marker.exists()  # recorded, never executed
    result = json.loads(summary["files"][1].read_text())
    assert result["provenance"]["info"]["activation"] == f"touch {marker}"


def test_allowlist_covers_loader_variables_and_a_project_can_extend_it(monkeypatch):
    env = {"LD_LIBRARY_PATH": "/opt/lib", "MY_TOOL_PREFIX": "/x", "SECRET_TOKEN": "s3cret"}
    assert snapshot.observed_context(env)["env"] == {"LD_LIBRARY_PATH": "/opt/lib"}
    env["BENCHX_ENV_ALLOWLIST"] = "MY_TOOL_PREFIX, ,LD_LIBRARY_PATH"
    seen = snapshot.observed_context(env)["env"]
    assert seen == {"LD_LIBRARY_PATH": "/opt/lib", "MY_TOOL_PREFIX": "/x"}  # never the whole environment
