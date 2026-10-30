"""`bx bench --adapter asv` and the asv adapter: a small suite in asv's language, timed by pyperf."""

import json
import os
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("pyperf")

from benchx import bench, cli, core, harnesses
from benchx.adapters import asv, asv_runtime
from benchx.store import Store

SUITE = textwrap.dedent('''
    class Sort:
        params = [[10, 100], ["a", "b"]]
        param_names = ["n", "key"]
        number = 3

        def setup(self, n, key):
            self.data = list(range(n))[::-1]

        def time_sorted(self, n, key):
            sorted(self.data)

        def peakmem_sorted(self, n, key):
            sorted(self.data)

    class Cached:
        def setup_cache(self):
            return list(range(50))

        def time_sum(self, data):
            sum(data)

    class WithSetup:
        def setup(self):
            self.data = list(range(100))

        def time_sum(self):
            sum(self.data)

    def time_plain():
        sum(range(100))

    class Skipped:
        def setup(self):
            raise NotImplementedError  # asv: skip this benchmark

        def time_skipped(self):
            pass

    def time_broken():
        raise ValueError("boom")

    class Mutating:
        number = 3

        def setup(self):
            self.data = [1]

        def time_pop(self):
            self.data.pop()  # fails unless every call sees a fresh setup

        def teardown(self):
            with open("teardowns", "a") as f:
                f.write("x")
''')
ASV = ["bench", ".", "--adapter", "asv"]
FAST = ["--processes", "2", "--values", "2", "--warmups", "0", "--min-time", "0.001"]


@pytest.fixture
def suite(tmp, monkeypatch):
    root = tmp / "proj"
    (root / "benchmarks").mkdir(parents=True)
    (root / "asv.conf.json").write_text('{\n  // a comment, as asv allows\n  "version": 1, "project": "json",\n'
                                        '  "benchmark_dir": "benchmarks"\n}\n')
    (root / "benchmarks" / "__init__.py").write_text("")
    (root / "benchmarks" / "bm.py").write_text(SUITE)
    (root / "benchmarks" / "needs_missing.py").write_text("import no_such_module_anywhere\n")
    monkeypatch.chdir(root)
    return root


def results(root, run_key):
    return {(d["coordinates"]["workload"]["name"], json.dumps(d["coordinates"]["workload"].get("parameters", {}),
                                                              sort_keys=True)): d
            for d in (json.loads(p.read_text()) for p in (root / "results" / run_key).glob("*.json"))
            if not d.get("schema_version") == "benchx/work-order/0.1.0"
            and d["measurement"].get("kind") != "condition"}  # the runner's load-average siblings


def test_flags_become_precision_and_overrides():
    precision = harnesses.ASV.flags_to_precision(processes=3, values=4, loops=5, warmups=0)
    applied, invocation = asv.protocol(precision)
    assert invocation == {"processes": 3, "values": 4, "loops": 5, "min_time": None, "warmups": 0}
    assert applied["repetitions"]["levels"] == [{"unit": "process", "n": 3}, {"unit": "value", "n": 4}]
    assert asv.protocol(asv.default_precision())[1] == {"processes": 2, "values": None, "loops": None,
                                                        "min_time": None, "warmups": None}
    with pytest.raises(bench.SessionError):
        harnesses.ASV.flags_to_precision(loops=1, min_time=0.1)
    with pytest.raises(asv.Unsupported):
        asv.protocol({"repetitions": 2})
    with pytest.raises(asv.Unsupported):
        asv.protocol({"repetitions": {"mode": "fixed", "levels": [{"unit": "process", "n": 1}]},
                      "warmup": {"mode": "time", "seconds": 1}})


def test_asv_target_reads_the_benchmark_dir(suite, tmp):
    assert bench.asv_target(suite) == (str(suite), "benchmarks")
    assert bench.asv_target(suite / "benchmarks") == (str(suite), "benchmarks")
    with pytest.raises(bench.SessionError, match="not inside the tree"):
        bench.asv_target(suite, tree=tmp / "elsewhere")
    with pytest.raises(bench.SessionError, match="no such benchmark directory"):
        bench.asv_target(tmp / "missing")


def test_asv_command_runs_every_time_benchmark(suite, capsys):
    assert cli.main([*ASV, "--run-key", "all", "--project", "p", *FAST]) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "skipped module benchmarks.needs_missing" in err
    assert "1 peakmem" in err
    found = results(suite, "all")
    for document in found.values():
        assert core.check(document, "measurement-result") == []
    # One case per parameter combination, parameters as asv's repr() strings.
    sort = {k[1]: d for k, d in found.items() if k[0] == "bm.Sort.time_sorted"}
    assert sorted(sort) == sorted(json.dumps({"n": n, "key": key}, sort_keys=True)
                                  for n in ("10", "100") for key in ("'a'", "'b'"))
    one = next(iter(sort.values()))
    assert one["measurement"]["status"] == "success"
    assert len(one["measurement"]["observations"]) == 4
    assert {o["group"] for o in one["measurement"]["observations"]} == {"0", "1"}
    protocol = one["coordinates"]["protocol"]
    assert protocol["harness"]["name"] == "pyperf"
    # The order's --values wins; the benchmark's own `number = 3` applies.
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 3}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 2}, {"unit": "value", "n": 2}]
    assert protocol["warmup"] == {"mode": "none"}
    assert one["procedure"]["inner_iterations"] == 3
    assert protocol["asv"]["benchmark_version"]
    assert one["provenance"]["info"]["asv"]["installed"]["name"] == "json"
    assert "env-mismatch" not in one.get("quality", {}).get("warnings", [])
    # setup_cache ran, and its value reached the benchmark.
    assert found[("bm.Cached.time_sum", "{}")]["measurement"]["status"] == "success"
    assert found[("bm.time_plain", "{}")]["measurement"]["status"] == "success"
    skipped = found[("bm.Skipped.time_skipped", "{}")]["measurement"]
    assert (skipped["status"], skipped["reason"]) == ("skipped", "harness.skipped")
    assert found[("bm.Mutating.time_pop", "{}")]["measurement"]["status"] == "success"
    broken = found[("bm.time_broken", "{}")]
    assert (broken["measurement"]["status"], broken["measurement"]["reason"]) == ("error", "harness.error")
    assert "boom" in broken["provenance"]["info"]["message"]
    assert not any("peakmem" in name for name, _ in found)
    assert {d["coordinates"]["workload"]["name"] for d in Store().documents("all")} >= {"bm.time_plain"}


def test_filter_and_benchmark_defaults(suite, capsys):
    assert cli.main([*ASV, "--filter", r"time_plain$", "--run-key", "one", "--no-ingest",
                     "--processes", "1"]) == cli.EXIT_OK
    found = results(suite, "one")
    assert list(found) == [("bm.time_plain", "{}")]
    document = found[("bm.time_plain", "{}")]
    protocol = document["coordinates"]["protocol"]
    # Nothing overridden: asv's defaults where pyperf can take them.
    assert protocol["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.01}
    assert protocol["warmup"] == {"mode": "count", "n_warmup": 1}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 1}, {"unit": "value", "n": 5}]
    assert {k: v for k, v in protocol["asv"].items() if k != "benchmark_version"} == {
        "values_from": "default", "timeout": 60.0}
    assert len(document["measurement"]["observations"]) == 5
    assert "1 rounds x per-benchmark repeat" in capsys.readouterr().err


def test_filter_matches_parameters_as_asv_does(suite):
    # asv searches `name(p0, p1)` per combination, so `time_sorted$` matches none of them.
    assert cli.main([*ASV, "--filter", r"time_sorted$|time_sorted\(100, 'b'\)|time_plain",
                     "--run-key", "params", "--no-ingest", *FAST]) == cli.EXIT_OK
    assert sorted(results(suite, "params")) == [
        ("bm.Sort.time_sorted", json.dumps({"key": "'b'", "n": "100"})), ("bm.time_plain", "{}")]


def test_cases_are_exact_names(suite):
    assert cli.main([*ASV, "bm.time_plain", "bm.Sort.time_sorted(10, 'a')", "--run-key", "exact", "--no-ingest",
                     *FAST]) == cli.EXIT_OK
    assert sorted(results(suite, "exact")) == [
        ("bm.Sort.time_sorted", json.dumps({"key": "'a'", "n": "10"})), ("bm.time_plain", "{}")]


def test_a_parametrized_benchmarks_name_selects_all_its_combinations(suite):
    """As `asv run --bench bm.Sort.time_sorted` does. The name stays exact: `names.time_sum`
    does not select `names.time_sum_fast`."""
    (suite / "benchmarks" / "names.py").write_text("def time_sum():\n    pass\n\n\ndef time_sum_fast():\n    pass\n")
    assert cli.main([*ASV, "bm.Sort.time_sorted", "names.time_sum", "--run-key", "bare", "--no-ingest",
                     *FAST]) == cli.EXIT_OK
    assert sorted(results(suite, "bare")) == sorted(
        [("bm.Sort.time_sorted", json.dumps({"key": key, "n": n})) for n in ("10", "100") for key in ("'a'", "'b'")]
        + [("names.time_sum", "{}")])


def test_a_benchmark_name_may_end_in_a_dash_and_digits(suite):
    """A parametrized case is `name-INDEX`, so a plain benchmark that asv names `time_dashed-1`
    (through `benchmark_name`) must not be read as combination 1 of `time_dashed`."""
    (suite / "benchmarks" / "dashed.py").write_text(
        "def time_dashed():\n    pass\n\n\ntime_dashed.benchmark_name = 'time_dashed-1'\n")
    assert cli.main([*ASV, "--filter", "time_dashed", "--run-key", "dashed", "--no-ingest", *FAST]) == cli.EXIT_OK
    found = results(suite, "dashed")
    assert list(found) == [("time_dashed-1", "{}")]
    assert found[("time_dashed-1", "{}")]["measurement"]["status"] == "success"
    assert "parameters" not in found[("time_dashed-1", "{}")]["coordinates"]["workload"]


def test_two_benchmarks_with_one_case_id_are_refused(suite):
    """`time_x` with two combinations is `time_x-0` and `time_x-1`; so is a plain `time_x-1`.
    The case id is how the runner finds the benchmark again, so the order is refused."""
    (suite / "benchmarks" / "clash.py").write_text(
        "class Params:\n    params = [1, 2]\n    param_names = ['n']\n\n    def time_x(self, n):\n        pass\n\n"
        "    time_x.benchmark_name = 'time_x'\n\n\n"
        "def time_plain():\n    pass\n\n\ntime_plain.benchmark_name = 'time_x-1'\n")
    runnable = asv.locate(suite, "benchmarks", dict(os.environ))
    with pytest.raises(asv.Unsupported, match="share the case id 'time_x-1'"):
        asv.list_cases(runnable, "time_x", dict(os.environ))


def test_env_var_named_like_a_parameter_is_refused(suite, capsys):
    # `-e key=x` would overwrite every combination's `key`, and the cases would overwrite each other's results.
    assert cli.main([*ASV, "--filter", "time_sorted", "-e", "key=x", "--run-key", "clash", "--no-ingest",
                     *FAST]) == cli.EXIT_REFUSED
    assert "environment variables named like workload parameters: key;" in capsys.readouterr().err
    assert results(suite, "clash") == {}


def test_setup_means_one_call_per_loop(suite):
    # As asv_runner >= 0.3.0 (asv#966, asv_runner#53): a benchmark with a setup and no number is timed one call per value.
    assert cli.main([*ASV, "--filter", r"WithSetup\.time_sum$", "--run-key", "c", "--no-ingest",
                     "--processes", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "c").values()
    protocol = document["coordinates"]["protocol"]
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 1}
    assert protocol["asv"]["setup_scope"] == "call"
    assert document["procedure"]["inner_iterations"] == 1
    # A sum of 100 ints is far below a value's 10 ms: pyperf would have batched it.
    assert document["quality"]["warnings"] == [asv.UNBATCHED]


def test_setup_and_teardown_run_between_calls(suite, tmp):
    # number = 3: each of the three calls of a value gets its own setup; the
    # pop would fail on the second otherwise. Teardown runs before each re-setup and at the end.
    out = tmp / "m"
    assert cli.main([*ASV, "--filter", r"Mutating", "--run-key", "m", "--no-ingest", "--out", str(out),
                     "--processes", "1", "--values", "2", "--warmups", "0"]) == cli.EXIT_OK
    (document,) = (d for d in (json.loads(p.read_text()) for p in out.glob("*.json") if "workorder" not in p.name)
                   if d["measurement"].get("kind") != "condition")
    assert document["measurement"]["status"] == "success"
    assert document["procedure"]["inner_iterations"] == 3


SCOPED = textwrap.dedent('''
    class Sample:
        setup_scope = "sample"

        def setup(self):
            self.data = list(range(100))

        def time_sum(self):
            sum(self.data)

    class Counted:
        setup_scope = "sample"

        def setup(self):
            self.calls = 0

        def time_count(self):
            self.calls += 1

        def teardown(self):
            import os
            with open(os.environ["CALLS_FILE"], "a") as f:
                f.write(f"{self.calls}\\n")

    class Bogus:
        setup_scope = "value"

        def setup(self):
            pass

        def time_nothing(self):
            pass
''')


def test_sample_scope_batches_calls(suite):
    # asv#1613: setup once per value, so pyperf calibrates loops and nothing is flagged.
    (suite / "benchmarks" / "scoped.py").write_text(SCOPED)
    assert cli.main([*ASV, "--filter", r"Sample\.time_sum$", "--run-key", "s", "--no-ingest",
                     "--processes", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "s").values()
    protocol = document["coordinates"]["protocol"]
    assert protocol["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.01}
    assert protocol["asv"]["setup_scope"] == "sample"
    assert document["procedure"]["inner_iterations"] > 1
    assert "quality" not in document


def test_sample_scope_shares_setup_within_a_value(suite, monkeypatch):
    # Every value sees one setup and `loops` calls on it; teardown records how many.
    (suite / "benchmarks" / "scoped.py").write_text(SCOPED)
    calls = suite / "calls"
    monkeypatch.setenv("CALLS_FILE", str(calls))
    out = suite / "counted"
    assert cli.main([*ASV, "--filter", r"Counted", "--run-key", "n", "--no-ingest", "--out", str(out),
                     "--processes", "1", "--values", "3", "--warmups", "1", "--loops", "4"]) == cli.EXIT_OK
    (document,) = (d for d in (json.loads(p.read_text()) for p in out.glob("*.json") if "workorder" not in p.name)
                   if d["measurement"].get("kind") != "condition")
    assert document["measurement"]["status"] == "success"
    assert len(document["measurement"]["observations"]) == 3
    # One warmup and three values, four calls each; under "call" every line would be 1.
    assert calls.read_text().split() == ["4"] * 4


def test_setup_scope_from_the_module(suite):
    # A module-level setup hook with a module-level scope, as a pandas-style suite would declare it.
    (suite / "benchmarks" / "modscope.py").write_text(textwrap.dedent('''
        def setup(*args):
            pass

        setup_scope = "sample"

        class NoOwnSetup:
            def time_sum(self):
                sum(range(100))
    '''))
    assert cli.main([*ASV, "--filter", r"NoOwnSetup", "--run-key", "m2", "--no-ingest",
                     "--processes", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "m2").values()
    assert document["coordinates"]["protocol"]["asv"]["setup_scope"] == "sample"
    assert document["procedure"]["inner_iterations"] > 1


def test_unknown_setup_scope_is_skipped(suite, capsys):
    (suite / "benchmarks" / "scoped.py").write_text(SCOPED)
    cli.main([*ASV, "--filter", r"Bogus", "--run-key", "b", "--no-ingest", *FAST])
    assert "asv: skipped scoped.Bogus.time_nothing: setup_scope must be one of 'call', 'sample', not 'value'" \
        in capsys.readouterr().err
    assert results(suite, "b") == {}


def test_timeout_is_the_benchmarks_own(suite):
    (suite / "benchmarks" / "slow.py").write_text("import time\n"
                                                  "class Slow:\n    timeout = 1\n"
                                                  "    def time_sleep(self):\n        time.sleep(3)\n")
    assert cli.main([*ASV, "--filter", r"Slow", "--run-key", "t", "--no-ingest", "--processes", "1",
                     "--values", "1", "--warmups", "0", "--loops", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "t").values()
    assert (document["measurement"]["status"], document["measurement"]["reason"]) == ("error", "timeout")


def test_discovery_agrees_with_asv_runner(suite):
    """Names, parameter strings, versions and setup_cache keys as asv itself reports them."""
    pytest.importorskip("asv_runner")
    import contextlib, io, subprocess, sys
    probe = textwrap.dedent("""
        import contextlib, io, json, sys
        sys.path.insert(0, sys.argv[2])
        import asv_runtime
        from asv_runner.discovery import disc_benchmarks
        from asv_runner._aux import update_sys_path
        root = sys.argv[1]
        ours = {b.name: [b.type, b.params, b.param_names, b.version, b.setup_cache_key]
                for b in asv_runtime.discover(root)[0]}
        update_sys_path(root)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            theirs = {b.name: [b.type, b.params, b.param_names, b.version, b.setup_cache_key]
                      for b in disc_benchmarks(root, ignore_import_errors=True)}
        print(json.dumps([ours, theirs]))
    """)
    out = subprocess.run([sys.executable, "-c", probe, str(suite / "benchmarks"), str(Path(asv_runtime.__file__).parent)],
                         cwd=suite, capture_output=True, text=True, check=True)
    ours, theirs = json.loads(out.stdout.splitlines()[-1])
    assert {k: v for k, v in ours.items() if k in theirs} == theirs
    assert set(ours) - set(theirs) <= {k for k, v in ours.items() if v[0] == "memory"}


def test_missing_asv_runner_is_refused(suite, monkeypatch, tmp, capsys):
    bare = tmp / "bin"
    bare.mkdir()
    (bare / "python").symlink_to("/usr/bin/false")
    monkeypatch.setenv("PATH", str(bare))
    assert cli.main([*ASV, "--no-ingest"]) == cli.EXIT_REFUSED
    assert "pyperf is not importable" in capsys.readouterr().err
