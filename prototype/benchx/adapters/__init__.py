"""Harness adapters (harness-adapter.md). The contract is in `base`."""

from . import gbench, pyperf

ADAPTERS = {gbench.NAME: gbench, pyperf.NAME: pyperf}


def get(name: str):
    """The adapter module registered under `name`, or None."""
    return ADAPTERS.get(name)
