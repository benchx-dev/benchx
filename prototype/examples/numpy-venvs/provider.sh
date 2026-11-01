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

# The request is one flat JSON object (schemas/target-provider-request), so its configuration is the
# string after that key. An absent or null one leaves this empty, and is refused below.
configuration=$(sed -n 's/.*"configuration"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
case "$configuration" in
  a) version=$NUMPY_A ;;
  b) version=$NUMPY_B ;;
  *) echo "provider: unknown configuration '$configuration' (a or b)" >&2; exit 2 ;;
esac
# The canonical URI this working tree's results are filed under, as in archery-local.
uri=https://example.org/numpy-venvs/demo.git

# A JSON string's body: the backslash and the quote escaped, which is all a path or a version needs.
json_string() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

cat <<JSON
{
 "schema_version": "benchx/target-description/0.1.0",
 "target": {
  "kind": "working_tree",
  "path": "$(json_string "$HERE")",
  "source": {"uri": "$(json_string "$uri")", "type": "git"}
 },
 "how_built": {"summary": {"numpy": "$(json_string "$version")"}},
 "provider": {"name": "provider.sh", "version": "1"}
}
JSON
