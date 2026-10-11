"""benchx prototype: the slice of docs/design/prototype-design.md.

One module per component behind the `bx` command line: core (reading and
checking documents), snapshot (what is recorded), adapters.gbench, runner,
identity, parquet, store, compare, session (the workbench's loop over runner
and comparator).
"""

__version__ = "0.1.0"
