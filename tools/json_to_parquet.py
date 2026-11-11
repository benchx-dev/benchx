#!/usr/bin/env python3
"""Write measurement-result JSON documents as the rows of one Parquet file.

Each document is read strictly and checked as `bx validate` checks it; the
rows are the message columns of benchx.parquet, whose Arrow schema is derived
from the shipped result schema, so this program cannot drift from it. See
docs/design/measurement-result-parquet.md.

    pip install benchx
    python tools/json_to_parquet.py results.parquet \
        src/benchx/schemas/measurement-result/0.1.0/examples/*.json
"""

import sys
from pathlib import Path

import pyarrow.parquet as pq

from benchx import core, parquet


def main(output, *paths):
    """Write the documents at `paths` to `output`; nothing is written if one is invalid."""
    documents, failed = [], False
    for path in paths:
        inspection = core.inspect(Path(path), "measurement-result")
        for error in inspection.errors:
            print(f"{path}: {error}", file=sys.stderr)
        failed |= not inspection.valid
        documents.append(inspection.data)
    if failed:
        return 1
    pq.write_table(parquet.message_table(documents), output, compression="zstd")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(f"usage: {sys.argv[0]} OUTPUT.parquet DOCUMENT.json...")
    sys.exit(main(*sys.argv[1:]))
