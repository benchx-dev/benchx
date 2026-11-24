"""The benchx implementation: the slice of docs/design/prototype-design.md.

One module per component behind the `bx` command line: core (reading and
checking documents), snapshot (what is recorded), adapters.gbench, runner,
identity, parquet, store, compare, session (the workbench's loop over runner
and comparator).
"""

import logging

__version__ = "0.2.0"

# A library caller sees no log output unless it configures logging itself; `bx` (cli.main)
# installs its own handlers (log.py).
logging.getLogger(__name__).addHandler(logging.NullHandler())
