#!/usr/bin/env bash
# The target provider of this example (system-decomposition.md §3.4): one JSON request on stdin,
# one target description on stdout. It builds nothing, so `describe` and `prepare` answer alike.
#
# Configuration `a` or `b` names a side. The description declares, in how_built, the numpy the
# user means that side to have, in the shape describe_env.py prints. The collector in
# benchx.conf.json compares the two: a side whose environment is not the one it was declared
# with gets the warning collector-mismatch on every result.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
: "${NUMPY_A:?run.sh sets NUMPY_A and NUMPY_B}" "${NUMPY_B:?run.sh sets NUMPY_A and NUMPY_B}"

configuration=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("configuration") or "")')
case "$configuration" in
  a) version=$NUMPY_A ;;
  b) version=$NUMPY_B ;;
  *) echo "provider: unknown configuration '$configuration' (a or b)" >&2; exit 2 ;;
esac
# The canonical URI this working tree's results are filed under, as in archery-local.
uri=https://example.org/numpy-venvs/demo.git

HERE=$HERE URI=$uri VERSION=$version python3 - <<'PY'
import json, os
print(json.dumps({
    "schema_version": "benchx/target-description/0.1.0",
    "target": {"kind": "working_tree", "path": os.environ["HERE"],
               "source": {"uri": os.environ["URI"], "type": "git"}},
    "how_built": {"summary": {"numpy": os.environ["VERSION"]}},
    "provider": {"name": "provider.sh", "version": "1"},
}, indent=1))
PY
