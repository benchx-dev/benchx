"""Mistakes that used to end in a traceback, a wrong message or a leaked value.

Each is a refusal the user can act on: one line, naming what to change.
"""

import json
import subprocess

import pytest

from benchx import cli, session
from benchx.adapters import asv, base, pyperf
from benchx.core import catalog
from benchx.results import MemoryStore

MEASURE = ["A", "B", "--profile", "revisions", "--suite", "s", "--rounds", "1", "--adapter", "pyperf"]


@pytest.mark.parametrize("item", ["hunter2", "=hunter2"])
def test_a_malformed_environment_item_is_not_echoed(item, capsys):
    status = cli.main(["compare", *MEASURE, "-e", item])
    err = capsys.readouterr().err
    assert status == cli.EXIT_REFUSED
    assert "-e wants NAME=VALUE" in err
    assert "hunter2" not in err


# ---- the probe's message names the last line of stderr, as text


def _probe_fails(monkeypatch, module, stderr):
    monkeypatch.setattr(module, "_which_python", lambda env: "/fake/python")
    monkeypatch.setattr(
        module, "run_bounded", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr=stderr)
    )


@pytest.mark.parametrize("module, suite_is_dir", [(pyperf, False), (asv, True)])
def test_the_pyperf_probe_message_is_a_line_not_a_list(module, suite_is_dir, tmp, monkeypatch):
    suite = tmp / "suite"
    suite.mkdir() if suite_is_dir else suite.write_text("")
    _probe_fails(monkeypatch, module, "Traceback (most recent call last):\nModuleNotFoundError: no pyperf\n")
    with pytest.raises(base.Unsupported) as e:
        module.locate(tmp, "suite", {})
    message = str(e.value)
    assert message.endswith("pyperf is not importable by /fake/python: ModuleNotFoundError: no pyperf")
    assert "[" not in message


@pytest.mark.parametrize("module, suite_is_dir", [(pyperf, False), (asv, True)])
def test_the_probe_message_survives_an_empty_stderr(module, suite_is_dir, tmp, monkeypatch):
    suite = tmp / "suite"
    suite.mkdir() if suite_is_dir else suite.write_text("")
    _probe_fails(monkeypatch, module, "")
    with pytest.raises(base.Unsupported, match="not importable by /fake/python: no output"):
        module.locate(tmp, "suite", {})


# ---- a listing that does not end in JSON is a refusal


def _listing_prints(monkeypatch, module, stdout):
    monkeypatch.setattr(
        module, "run_bounded", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr="")
    )


@pytest.mark.parametrize(
    "stdout, found",
    [
        ("", "it printed nothing"),
        ("[]\nbye from atexit\n", "'bye from atexit'"),
        ("not json at all", "'not json at all'"),
    ],
)
def test_a_pyperf_listing_that_does_not_end_in_json_is_refused(stdout, found, tmp, monkeypatch):
    (tmp / "m.py").write_text("")
    _listing_prints(monkeypatch, pyperf, stdout)
    runnable = {"python": "/fake/python", "module": tmp / "m.py", "root": tmp}
    with pytest.raises(base.Unsupported, match="cannot read the benchmark listing of m.py") as e:
        pyperf.list_cases(runnable, None, {})
    assert found in str(e.value)


@pytest.mark.parametrize(
    "stdout, found",
    [
        ("", "it printed nothing"),
        ('{"failed": []}\nbye\n', "'bye'"),
    ],
)
def test_an_asv_listing_that_does_not_end_in_json_is_refused(stdout, found, tmp, monkeypatch):
    _listing_prints(monkeypatch, asv, stdout)
    runnable = {"python": "/fake/python", "benchmark_dir": tmp, "root": tmp}
    with pytest.raises(base.Unsupported, match="cannot read the benchmark listing of") as e:
        asv.list_cases(runnable, None, {})
    assert found in str(e.value)


# ---- a filter that is not a regular expression is a refusal, before anything is started


def _nothing_may_start(*args, **kwargs):
    raise AssertionError("a listing process was started for an invalid filter")


@pytest.mark.parametrize(
    "module, runnable",
    [
        (pyperf, {"python": "/fake/python", "module": "m.py", "root": "."}),
        (asv, {"python": "/fake/python", "benchmark_dir": ".", "root": "."}),
    ],
)
def test_an_invalid_regular_expression_filter_is_refused(module, runnable, monkeypatch):
    monkeypatch.setattr(module, "run_bounded", _nothing_may_start)
    with pytest.raises(base.Unsupported, match=r"the case filter '\(' is not a regular expression"):
        module.list_cases(runnable, "(", {})


# ---- `bx compare` honours the configured order timeout, as `bx bench` does


@pytest.mark.parametrize("profile", ["revisions", "environments"])
def test_compare_targets_passes_the_configured_order_timeout(profile, tmp, monkeypatch):
    seen = []

    def stop(*args, **kwargs):
        seen.append(kwargs["timeout"])
        raise session.SessionError("stop here")

    monkeypatch.setattr(session, "_side", stop)
    with pytest.raises(session.SessionError, match="stop here"):
        session.compare_targets(
            "A",
            "B",
            profile=profile,
            suite="s",
            rounds=1,
            adapter="pyperf",
            label=("env", "x", "y") if profile == "environments" else None,
            store=MemoryStore(),
            order_s=7,
        )
    assert seen == [7]


# ---- a label name with a '/' is refused before any round runs, and as a refusal in read mode


def test_a_label_name_with_a_slash_is_refused_before_measuring(capsys):
    argv = [
        "compare",
        "nowhere",
        "nowhere",
        "--profile",
        "environments",
        "--label",
        "a/b=x,y",
        "--suite",
        "s",
        "--rounds",
        "1",
        "--adapter",
        "pyperf",
    ]
    assert cli.main(argv) == cli.EXIT_REFUSED
    assert "must not contain '/'" in capsys.readouterr().err


def test_a_label_name_with_a_slash_is_a_compare_error_in_read_mode():
    from benchx import compare

    with pytest.raises(compare.CompareError, match="must not contain '/'"):
        compare.profile_projection("environments", "a/b")


def test_bx_bench_refuses_a_label_name_with_a_slash_as_it_records_it(capsys):
    """Otherwise the results are recorded and ingested, and `bx compare --label a/b` then refuses them."""
    with pytest.raises(session.SessionError) as expected:
        session.parse_label("a/b=x,y")
    argv = [
        "bench",
        "nowhere",
        "--suite",
        "s",
        "--adapter",
        "pyperf",
        "--run-key",
        "k",
        "--slot",
        "0",
        "--label",
        "a/b=x",
    ]
    assert cli.main(argv) == cli.EXIT_REFUSED
    assert f"bx bench: error: {expected.value}" in capsys.readouterr().err


# ---- missing jsonschema format checker in $BENCHX_SCHEMAS is refused, not a traceback


def test_missing_format_checker_in_custom_schemas_is_refused(tmp_path, adhoc, monkeypatch, capsys):
    """A custom $BENCHX_SCHEMAS declaring an unavailable format (e.g. `iri`) returns EXIT_REFUSED
    with a single error line, not a traceback or EXIT_INVALID."""
    schemas = tmp_path / "schemas"
    dest = schemas / "measurement-result" / "0.1.0"
    dest.mkdir(parents=True)
    schema = json.loads((catalog.SCHEMAS / "measurement-result" / "0.1.0" / "schema.json").read_text())
    schema["$defs"]["uri"]["format"] = "no-such-custom-format"
    (dest / "schema.json").write_text(json.dumps(schema))

    doc_dir = tmp_path / "results"
    doc_dir.mkdir()
    doc_path = doc_dir / "result.json"
    doc_path.write_text(json.dumps(adhoc))

    monkeypatch.setattr(catalog, "SCHEMAS", schemas)
    catalog.clear_cache()
    try:
        assert cli.main(["validate", str(doc_path)]) == cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "bx validate: error:" in err and "no-such-custom-format" in err and "Traceback" not in err

        assert (
            cli.main(
                [
                    "compare",
                    "--run",
                    adhoc["provenance"]["run_key"],
                    "--profile",
                    "revisions",
                    "--baseline",
                    adhoc["revision"]["key"][:8],
                    "--results",
                    str(doc_dir),
                ]
            )
            == cli.EXIT_REFUSED
        )
        err = capsys.readouterr().err
        assert "bx compare: error:" in err and "no-such-custom-format" in err and "Traceback" not in err
    finally:
        catalog.clear_cache()


# ---- uniform error hierarchy and CLI refusal formatting


def test_domain_errors_subclass_benchx_error():
    from benchx import config, evidence, orders, projection, target, views
    from benchx.errors import BenchxError, SessionError, UsageError

    for exc_type in (
        UsageError,
        SessionError,
        config.ConfigError,
        orders.OrderError,
        target.TargetError,
        evidence.CompareError,
        projection.ProjectionError,
        views.ViewError,
    ):
        assert issubclass(exc_type, BenchxError)


def test_compare_and_target_refusals_use_uniform_cli_error_prefix(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["compare", "--run", "missing", "--profile", "revisions", "--baseline", "abc"]) == cli.EXIT_REFUSED
    assert "bx compare: error: no results for run 'missing'" in capsys.readouterr().err

    assert cli.main(["target", "describe", "@missing"]) == cli.EXIT_REFUSED
    assert 'bx target: error: @missing needs "provider"' in capsys.readouterr().err
