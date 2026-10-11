#!/usr/bin/env bash
# Run the example suite with `bx bench`. See README.md for what each step does.
#
#   ./run.sh                 uses .benchx/config.json here (5 processes x 3 values)
#   ./run.sh --processes 20  extra arguments go to `bx bench` and win over the file
#
# Needs Python >= 3.11 with pyperf, pyarrow, jsonschema, referencing, rfc8785.
# PYTHON=/path/to/python picks the interpreter. Results go to ./out, and the
# local store to ./out/home (BENCHX_HOME), so nothing outside this directory changes.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
export PYTHONPATH="$PWD/../..${PYTHONPATH:+:$PYTHONPATH}"
export BENCHX_HOME="$PWD/out/home"
bx() { printf '\033[36m$ bx %s\033[0m\n' "$*" >&2; "$PYTHON" -m benchx.cli "$@"; }

"$PYTHON" -c "import pyperf, pyarrow, jsonschema, referencing, rfc8785" 2>/dev/null || {
  echo "missing dependencies: $PYTHON -m pip install pyperf pyarrow jsonschema referencing rfc8785" >&2
  exit 1; }
rm -rf out; mkdir -p out

echo "== 1. all bench_* functions, precision from .benchx/config.json"
bx bench bench_suite.py --run-key example-all --out out/all "$@"

echo; echo "== 2. one function, an environment variable set for the workers, flags over the file"
bx bench bench_suite.py bench_env_var -e EXAMPLE_N=100000 --processes 3 --values 2 \
  --run-key example-env --out out/env

echo; echo "== 3. what is in the local store"
bx head -n 10

echo; echo "== 4. what was written"
find out/all out/env -type f | sort | sed 's/^/  /'
