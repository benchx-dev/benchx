"""`bx asv` and the asv adapter: a small suite in asv's language, timed by pyperf."""

import json
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
FAST = ["--rounds", "2", "--repeat", "2", "--warmups", "0", "--sample-time", "0.001"]


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
            if not d.get("schema_version") == "benchx/work-order/0.1.0"}


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


def test_asv_suite_reads_the_benchmark_dir(suite):
    assert bench.asv_suite(suite) == suite / "benchmarks"
    assert bench.asv_suite(suite / "benchmarks") == suite / "benchmarks"


def test_asv_command_runs_every_time_benchmark(suite, capsys):
    assert cli.main(["asv", "--run-key", "all", "--project", "p", *FAST]) == cli.EXIT_OK
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
    context = one["coordinates"]["comparison_context"]
    assert context["harness"]["name"] == "pyperf"
    protocol = context["protocol"]
    # The order's --repeat wins; the benchmark's own `number = 3` applies.
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 3}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 2}, {"unit": "value", "n": 2}]
    assert protocol["warmup"] == {"mode": "none"}
    assert one["procedure"]["inner_iterations"] == 3
    assert context["benchmark_version"]
    assert one["observed_context"]["asv"]["project"]["name"] == "json"
    assert "env-mismatch" not in one.get("quality", {}).get("warnings", [])
    # setup_cache ran, and its value reached the benchmark.
    assert found[("bm.Cached.time_sum", "{}")]["measurement"]["status"] == "success"
    assert found[("bm.time_plain", "{}")]["measurement"]["status"] == "success"
    assert found[("bm.Skipped.time_skipped", "{}")]["measurement"] == {"status": "skipped",
                                                                       "reason": "harness.skipped"}
    assert found[("bm.Mutating.time_pop", "{}")]["measurement"]["status"] == "success"
    broken = found[("bm.time_broken", "{}")]
    assert broken["measurement"] == {"status": "error", "reason": "harness.error"}
    assert "boom" in broken["provenance"]["info"]["message"]
    assert not any("peakmem" in name for name, _ in found)
    assert {d["coordinates"]["workload"]["name"] for d in Store().documents("all")} >= {"bm.time_plain"}


def test_filter_and_benchmark_defaults(suite, capsys):
    assert cli.main(["asv", ".", "-b", r"time_plain$", "--run-key", "one", "--no-ingest",
                     "--rounds", "1"]) == cli.EXIT_OK
    found = results(suite, "one")
    assert list(found) == [("bm.time_plain", "{}")]
    document = found[("bm.time_plain", "{}")]
    protocol = document["coordinates"]["comparison_context"]["protocol"]
    # Nothing overridden: asv's defaults where pyperf can take them.
    assert protocol["calibration"] == {"mode": "adaptive", "minimum_sample_seconds": 0.01}
    assert protocol["warmup"] == {"mode": "count", "n_warmup": 1}
    assert protocol["repetitions"]["levels"] == [{"unit": "process", "n": 1}, {"unit": "value", "n": 5}]
    assert protocol["asv"] == {"values_from": "default", "timeout": 60.0, "setup_per_call": False}
    assert len(document["measurement"]["observations"]) == 5
    assert "1 rounds x per-benchmark repeat" in capsys.readouterr().err


def test_filter_matches_parameters_as_asv_does(suite):
    # asv searches `name(p0, p1)` per combination, so `time_sorted$` matches none of them, and `-b` repeats.
    assert cli.main(["asv", ".", "-b", r"time_sorted$", "-b", r"time_sorted\(100, 'b'\)", "-b", "time_plain",
                     "--run-key", "params", "--no-ingest", *FAST]) == cli.EXIT_OK
    assert sorted(results(suite, "params")) == [
        ("bm.Sort.time_sorted", json.dumps({"key": "'b'", "n": "100"})), ("bm.time_plain", "{}")]


def test_env_var_named_like_a_parameter_is_refused(suite, capsys):
    # `-e key=x` would overwrite every combination's `key`, and the cases would overwrite each other's results.
    assert cli.main(["asv", ".", "-b", "time_sorted", "-e", "key=x", "--run-key", "clash", "--no-ingest",
                     *FAST]) == cli.EXIT_REFUSED
    assert "environment variables named like workload parameters: key;" in capsys.readouterr().err
    assert results(suite, "clash") == {}


def test_setup_means_one_call_per_loop(suite):
    # asv#966: a benchmark with a setup and no declared number is timed one call per value.
    assert cli.main(["asv", "-b", r"WithSetup\.time_sum$", "--run-key", "c", "--no-ingest",
                     "--rounds", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "c").values()
    protocol = document["coordinates"]["comparison_context"]["protocol"]
    assert protocol["calibration"] == {"mode": "fixed", "n_iterations": 1}
    assert protocol["asv"]["setup_per_call"] is True
    assert document["procedure"]["inner_iterations"] == 1


def test_setup_and_teardown_run_between_calls(suite, tmp):
    # number = 3: each of the three calls of a value gets its own setup; the
    # pop would fail on the second otherwise. Teardown runs before each re-setup and at the end.
    out = tmp / "m"
    assert cli.main(["asv", "-b", r"Mutating", "--run-key", "m", "--no-ingest", "--out", str(out),
                     "--rounds", "1", "--repeat", "2", "--warmups", "0"]) == cli.EXIT_OK
    (document,) = (json.loads(p.read_text()) for p in out.glob("*.json") if "workorder" not in p.name)
    assert document["measurement"]["status"] == "success"
    assert document["procedure"]["inner_iterations"] == 3


def test_timeout_is_the_benchmarks_own(suite):
    (suite / "benchmarks" / "slow.py").write_text("import time\n"
                                                  "class Slow:\n    timeout = 1\n"
                                                  "    def time_sleep(self):\n        time.sleep(3)\n")
    assert cli.main(["asv", "-b", r"Slow", "--run-key", "t", "--no-ingest", "--rounds", "1",
                     "--repeat", "1", "--warmups", "0", "--number", "1"]) == cli.EXIT_OK
    (document,) = results(suite, "t").values()
    assert document["measurement"] == {"status": "error", "reason": "timeout"}


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
    assert cli.main(["asv", "--no-ingest"]) == cli.EXIT_REFUSED
    assert "pyperf is not importable" in capsys.readouterr().err
