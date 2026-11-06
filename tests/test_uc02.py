"""UC-02: two functions of one benchmark compared, with the rounds interleaved.

The two functions are the two values of one workload parameter (#94's `params` and
`param_names`). Each `bx bench` call runs one value as one side of a session, placed by
--run-key, --round and --slot; the comparison is `bx compare --run ... --profile variants`.
A value is slower on purpose, so that its effect has a sign whatever the noise.
"""

import json
import textwrap

import pytest

pytest.importorskip("pyperf")

from benchx import cli, core
from conftest import git

RUN = "uc02"
BASELINE, VARIANT = "'baseline'", "'variant'"
PYPERF = ["--processes", "2", "--values", "2", "--loops", "20", "--no-project"]
ASV = ["--processes", "2", "--values", "2", "--warmups", "0", "--min-time", "0.001", "--no-project"]

# §2 of the plan: one function, and the two values of `role` select what it runs.
ONE_PARAMETER = textwrap.dedent('''
    def bench_truncate(role):
        sum(range(10 if role == "baseline" else 2000))

    bench_truncate.params = ["baseline", "variant"]
    bench_truncate.param_names = ["role"]
''')
TWO_PARAMETERS = textwrap.dedent('''
    def bench_truncate(role, size):
        sum(range(size if role == "baseline" else 20 * size))

    bench_truncate.params = [["baseline", "variant"], [10, 100]]
    bench_truncate.param_names = ["role", "size"]
''')


def make_tree(root, files):
    """A git repository with `files` committed and nothing else: no .gitignore."""
    root.mkdir()
    git(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "suite")
    return root


@pytest.fixture
def tree(tmp, monkeypatch):
    path = make_tree(tmp / "tree", {"mod.py": ONE_PARAMETER})
    monkeypatch.chdir(path)
    return path


def loop(target, selectors, fast, rounds=3, out=False):
    """The caller's loop (§3): one `bx bench` call per value per round, in the same order every round."""
    slot = 0
    for round_ in range(rounds):
        for selector in selectors:
            argv = ["bench", *target, *selector, "--run-key", RUN, "--round", str(round_), "--slot", str(slot), *fast]
            if out:
                argv += ["--out", f"out/call-{slot}"]
            assert cli.main(argv) == cli.EXIT_OK
            slot += 1


def compare(capsys, *extra, baseline=BASELINE):
    """`bx compare --run`: the exit code, the comparison document (None if not printed) and stderr."""
    capsys.readouterr()
    code = cli.main(["compare", "--run", RUN, "--profile", "variants", "--baseline", baseline, "--json", *extra])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def by_case(selector_values):
    """One selector per value, naming one case each: `bx bench mod.py "bench_truncate('baseline')"`."""
    return [[f"bench_truncate({value})"] for value in selector_values]


def test_the_loop_compares_the_two_values(tree, capsys):
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF)
    code, doc, err = compare(capsys)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert core.check(doc, "comparison-document") == []
    assert "varying: role (the only parameter)" in err  # inferred, and said so
    assert doc["varying"] == "coordinates.workload.parameters.role"
    assert (doc["baseline"], doc["contender"]) == (BASELINE, VARIANT)
    [unit] = doc["units"]
    assert unit["workload"] == {"name": "bench_truncate"} and unit["rounds"] == 3 and unit["effect"] > 0
    assert doc["local_only"] is False  # a clean tree: the store and the results are not part of it


def test_the_baseline_can_be_the_other_value(tree, capsys):
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF)
    code, doc, _ = compare(capsys, baseline=VARIANT)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert (doc["baseline"], doc["contender"]) == (VARIANT, BASELINE)
    assert doc["units"][0]["effect"] < 0


def test_an_unquoted_baseline_is_refused_with_the_recorded_values(tree, capsys):
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF, rounds=2)
    code, doc, err = compare(capsys, baseline="baseline")
    assert code == cli.EXIT_REFUSED and doc is None
    assert "'baseline'" in err and "'variant'" in err


def test_a_second_parameter_makes_one_unit_per_value_of_it(tmp, monkeypatch, capsys):
    monkeypatch.chdir(make_tree(tmp / "tree", {"mod.py": TWO_PARAMETERS}))
    loop(["mod.py"], [["--filter", BASELINE], ["--filter", VARIANT]], PYPERF)
    code, doc, err = compare(capsys)  # two parameters: it cannot be guessed
    assert code == cli.EXIT_REFUSED and doc is None
    assert "several workload parameters (role, size): give --vary NAME" in err
    code, doc, err = compare(capsys, "--vary", "role")
    assert code == cli.EXIT_OK and doc["failed_invariants"] == [] and "varying:" not in err
    assert [u["workload"] for u in doc["units"]] == [
        {"name": "bench_truncate", "parameters": {"size": "10"}},
        {"name": "bench_truncate", "parameters": {"size": "100"}}]
    assert all(u["effect"] > 0 for u in doc["units"])
    cli.main(["compare", "--run", RUN, "--profile", "variants", "--baseline", BASELINE, "--vary", "role"])
    shown = capsys.readouterr().out
    assert "bench_truncate [size=10]" in shown and "bench_truncate [size=100]" in shown


def test_a_vary_that_names_no_parameter_of_the_results_fails_the_comparison(tree, capsys):
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF, rounds=2)
    code, doc, _ = compare(capsys, "--vary", "size")
    assert code == cli.EXIT_OK and doc["units"] == []
    assert [f["invariant"] for f in doc["failed_invariants"]] == ["sides"]
    assert "no workload parameter size" in doc["failed_invariants"][0]["detail"]


def test_one_bench_call_is_not_an_alternating_run(tree, capsys):
    """Both values in one call: one order, no round and no slot. The refusal says how to alternate them."""
    assert cli.main(["bench", "mod.py", "--run-key", RUN, *PYPERF]) == cli.EXIT_OK
    code, doc, _ = compare(capsys)
    assert code == cli.EXIT_OK and doc["units"] == []
    assert [f["invariant"] for f in doc["failed_invariants"]] == ["alternating-slots"]
    detail = doc["failed_invariants"][0]["detail"]
    assert "--round and --slot" in detail and "bx compare TARGET --profile variants" in detail


def test_a_directory_per_call_inside_the_tree_leaves_the_tree_alone(tree, capsys):
    """`--out out/NAME` per call, in a repository with no .gitignore (§5.0): the tree id stays the same."""
    assert not (tree / ".gitignore").exists()
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF, out=True)
    assert (tree / "out" / "call-0").is_dir() and (tree / "out" / "call-5").is_dir()
    code, doc, _ = compare(capsys)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert doc["local_only"] is False and len(doc["units"]) == 1


def test_an_uncommitted_edit_is_compared_and_named(tree, capsys):
    (tree / "mod.py").write_text(ONE_PARAMETER + "# edited\n")
    loop(["mod.py"], by_case([BASELINE, VARIANT]), PYPERF, rounds=2)
    code, doc, _ = compare(capsys, "--min-rounds", "2")
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    # The suite is in the subject's tree, so the benchmark is dirty as the subject is (#89).
    assert doc["local_only"] is True and set(doc["units"][0]["caveats"]) == {
        f"{side}: dirty-{what}-tree" for side in ("baseline", "contender") for what in ("subject", "benchmark")}


# --- the same with an asv benchmark directory ---------------------------------------------------

ASV_CONF = '{"version": 1, "project": "uc02", "benchmark_dir": "benchmarks"}\n'
ASV_ONE = textwrap.dedent('''
    def time_truncate(role):
        sum(range(10 if role == "baseline" else 2000))

    time_truncate.params = ["baseline", "variant"]
    time_truncate.param_names = ["role"]
''')
ASV_TWO = textwrap.dedent('''
    def time_truncate(role, size):
        sum(range(size if role == "baseline" else 20 * size))

    time_truncate.params = [["baseline", "variant"], [10, 100]]
    time_truncate.param_names = ["role", "size"]
''')


def asv_tree(tmp, monkeypatch, source):
    path = make_tree(tmp / "proj", {"asv.conf.json": ASV_CONF, "benchmarks/__init__.py": "",
                                    "benchmarks/trunc.py": source})
    monkeypatch.chdir(path)
    return path


def test_the_loop_with_an_asv_suite_names_cases_by_module(tmp, monkeypatch, capsys):
    """An asv case name carries its module: `trunc.time_truncate('baseline')`, and so does the unit."""
    asv_tree(tmp, monkeypatch, ASV_ONE)
    loop([".", "--adapter", "asv"], [[f"trunc.time_truncate({v})"] for v in (BASELINE, VARIANT)], ASV)
    code, doc, err = compare(capsys)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert "varying: role (the only parameter)" in err
    [unit] = doc["units"]
    assert unit["workload"] == {"name": "trunc.time_truncate"} and unit["effect"] > 0


def test_the_loop_with_an_asv_suite_and_a_second_parameter(tmp, monkeypatch, capsys):
    asv_tree(tmp, monkeypatch, ASV_TWO)
    loop([".", "--adapter", "asv"], [["--filter", BASELINE], ["--filter", VARIANT]], ASV)
    code, doc, _ = compare(capsys, "--vary", "role")
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert [u["workload"] for u in doc["units"]] == [
        {"name": "trunc.time_truncate", "parameters": {"size": "10"}},
        {"name": "trunc.time_truncate", "parameters": {"size": "100"}}]


# --- measure mode: the same loop in one command (§1) --------------------------------------------

def measure(capsys, *extra):
    capsys.readouterr()
    code = cli.main(["compare", ".", "--profile", "variants", "--suite", "mod.py", "--adapter", "pyperf",
                     "--rounds", "3", "--run-key", RUN, "--json", *PYPERF, *extra])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def test_measure_mode_runs_the_loop_in_one_command_and_equals_read_mode(tree, capsys):
    code, doc, err = measure(capsys)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == [] and core.check(doc, "comparison-document") == []
    assert "varying: role (the only parameter)" in err and "round 3/3 role='variant'" in err
    assert (doc["baseline"], doc["contender"]) == (BASELINE, VARIANT) and doc["local_only"] is False
    [unit] = doc["units"]
    assert unit["workload"] == {"name": "bench_truncate"} and unit["rounds"] == 3 and unit["effect"] > 0
    # What the session stored compares to the same document when read back (criterion 8).
    code, again, _ = compare(capsys)
    assert code == cli.EXIT_OK and again == doc


def test_measure_mode_takes_the_baseline_value(tree, capsys):
    code, doc, _ = measure(capsys, "--baseline", VARIANT)
    assert code == cli.EXIT_OK and doc["failed_invariants"] == []
    assert (doc["baseline"], doc["contender"]) == (VARIANT, BASELINE) and doc["units"][0]["effect"] < 0


def test_measure_mode_refuses_a_baseline_that_is_not_a_value_before_running(tree, capsys):
    code, doc, err = measure(capsys, "--baseline", "variant")  # the recorded value has its quotes
    assert code == cli.EXIT_REFUSED and doc is None
    assert "baseline 'variant' is not a value of role; use one of: 'baseline', 'variant'" in err
    assert not (tree / "results").exists()  # nothing ran
