#!/usr/bin/env bash
# Run the example suite with `bx bench`. See README.md for what each step does.
#
#   ./run.sh                 uses benchx.conf.json here (5 processes x 3 values)
#   ./run.sh --processes 20  extra arguments go to `bx bench` and win over the file
#
# Run it in an activated environment that has benchx installed with pyperf
# (from prototype/: `pip install -e . pyperf`, or `uv sync --extra test`).
# Results go to ./out, and the local store to ./out/store.parquet (both set in
# benchx.conf.json), so nothing outside this directory changes.
set -euo pipefail
cd "$(dirname "$0")"
bx() { printf '\033[36m$ bx %s\033[0m\n' "$*" >&2; command bx "$@"; }

command bx --help >/dev/null 2>&1 && python -c "import pyperf" 2>/dev/null || {
  echo "missing dependencies: install benchx and pyperf into the active environment (see README.md)" >&2
  exit 1; }
rm -rf out; mkdir -p out

echo "== 1. all bench_* functions, suite and precision from benchx.conf.json"
bx bench . --run-key example-all --out out/all "$@"

echo; echo "== 2. one function, an environment variable set for the workers, flags over the file"
bx bench . bench_env_var -e EXAMPLE_N=100000 --processes 3 --values 2 \
  --run-key example-env --out out/env

echo; echo "== 3. the same case again under a new run key, so its series gets a second point"
bx bench . bench_join --run-key example-again --out out/again

echo; echo "== 4. what is in the local store: one Parquet file"
ls -l out/store.parquet
bx head -n 10

echo; echo "== 5. its series (bench_env_var has two: the -e setting changes the environment)"
bx series --quantity wall-time > out/series.txt
cat out/series.txt

echo; echo "== 6. the history of bench_join: one point per run"
bx series --workload bench_join --quantity wall-time > out/join-series.txt
SERIES=$(awk 'NR == 1 {print $1}' out/join-series.txt)
[ -n "$SERIES" ] || { echo "no bench_join series in the store" >&2; exit 1; }
bx history "$SERIES" > out/history.txt
cat out/history.txt
[ "$(grep -c example- out/history.txt)" -eq 2 ] || { echo "expected two history points for $SERIES" >&2; exit 1; }

echo; echo "== 7. what was written"
find out/all out/env out/again -type f | sort | sed 's/^/  /'
