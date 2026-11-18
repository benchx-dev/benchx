"""Mistakes that used to end in a traceback, a wrong message or a leaked value.

Each is a refusal the user can act on: one line, naming what to change.
"""

import pytest

from benchx import cli

MEASURE = ["A", "B", "--profile", "revisions", "--suite", "s", "--rounds", "1", "--adapter", "pyperf"]


@pytest.mark.parametrize("item", ["hunter2", "=hunter2"])
def test_a_malformed_environment_item_is_not_echoed(item, capsys):
    status = cli.main(["compare", *MEASURE, "-e", item])
    err = capsys.readouterr().err
    assert status == cli.EXIT_REFUSED
    assert "-e wants NAME=VALUE" in err
    assert "hunter2" not in err
