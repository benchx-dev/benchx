#!/usr/bin/env python3
"""The target provider of this example (system-decomposition.md §3.4): one JSON request on stdin,
one target description on stdout. It builds nothing, so `describe` and `prepare` answer alike.

Configuration `a` or `b` names a side. The description declares, in how_built, the numpy the
user means that side to have, in the shape describe_env.py prints. The collector in
benchx.conf.json compares the two: a side whose environment is not the one it was declared
with gets the warning collector-mismatch on every result.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The canonical URI this working tree's results are filed under, as in archery-local.
URI = "https://example.org/numpy-venvs/demo.git"


def main():
    versions = {"a": os.environ.get("NUMPY_A"), "b": os.environ.get("NUMPY_B")}
    if not all(versions.values()):
        sys.exit("provider: run.sh sets NUMPY_A and NUMPY_B")

    # An absent or null configuration is refused below, like an unknown one.
    configuration = json.load(sys.stdin).get("configuration")
    if configuration not in versions:
        print(f"provider: unknown configuration {configuration!r} (a or b)", file=sys.stderr)
        sys.exit(2)

    json.dump({
        "schema_version": "benchx/target-description/0.1.0",
        "target": {
            "kind": "working_tree",
            "path": str(HERE),
            "source": {"uri": URI, "type": "git"},
        },
        "how_built": {"summary": {"numpy": versions[configuration]}},
        "provider": {"name": "provider.py", "version": "1"},
    }, sys.stdout, indent=1)
    print()


main()
