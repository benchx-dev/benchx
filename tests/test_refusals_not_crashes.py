"""Mistakes that used to end in a traceback, a wrong message or a leaked value.

Each is a refusal the user can act on: one line, naming what to change.
"""

import subprocess

import pytest

from benchx import cli
from benchx.adapters import asv, base, pyperf

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
    monkeypatch.setattr(module, "run_bounded", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout="", stderr=stderr))


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
