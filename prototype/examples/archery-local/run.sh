#!/usr/bin/env bash
# End to end, in the shape of the Arrow local story ("Measure what a build
# option costs"): one commit built two ways, plain and hardened, compared with
# noise-aware verdicts, where the comparison describes itself.
#
# You play the user. The target provider (provider.sh, named in
# .benchx/provider.json) is your own build script: it builds with CMake and
# tells benchx what it built, how, and what environment you need. benchx builds
# nothing, and runs none of that text: you activate the environment yourself
# (activate.sh) and benchx records the text beside the environment it inherited.
#
# Every command is printed before it runs, as you would type it; `bx` stands
# for `$PYTHON -m benchx.cli`. Needs cmake and a C++ compiler on PATH, and
# network access the first time, to fetch Google Benchmark unless installed.
#
#   ROUNDS=5  alternation rounds (default 5)
#   PYTHON    the interpreter with benchx installed (default ../../.venv/bin/python)
#
# The local store is kept inside this directory (_work/benchx-home), not in
# ~/.benchx. Delete _work/ to start over.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD
PYTHON=${PYTHON:-$HERE/../../.venv/bin/python}
ROUNDS=${ROUNDS:-5}
export BENCHX_HOME=${BENCHX_HOME:-$HERE/_work/benchx-home}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RUN="hardening-$STAMP"

for tool in cmake c++; do
  command -v "$tool" >/dev/null || { echo "run.sh: $tool not found on PATH (see README.md)" >&2; exit 1; }
done
"$PYTHON" -c 'import benchx' 2>/dev/null || { echo "run.sh: benchx is not installed for $PYTHON (see README.md)" >&2; exit 1; }

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
say() { printf '\033[36m$ %s\033[0m\n' "$*" >&2; }
bx() { say bx "$@"; "$PYTHON" -m benchx.cli "$@"; }

step "user: activate the environment (benchx will not do this for you)"
say source activate.sh
source activate.sh
echo "LD_LIBRARY_PATH=$LD_LIBRARY_PATH"

step "ask the provider about a target that is not built yet: describe builds nothing"
bx target describe @plain || echo "(exit $?: nothing to describe yet, as expected)"

step "prepare both configurations: the provider builds (its build output is the dimmed lines)"
bx target prepare @plain > /dev/null
bx target prepare @hardened

step "describe now works, and the description lives in the build directory too"
bx target describe @hardened | head -3
say head -3 _work/build-hardened/.benchx-target.json
head -3 _work/build-hardened/.benchx-target.json
echo "(the same document: a build directory carries its own description)"

step "compare: the workbench asks the provider to prepare each side (a no-op), then alternates them"
bx compare @plain @hardened --profile environments --label build=plain,hardened \
  --suite demo-bench --rounds "$ROUNDS" --project archery-local --run-key "$RUN" --out _work/results

step "the comparison names what was built and how; before, it was two opaque directory paths"
echo "# the first work order's target, as the runner received it:"
"$PYTHON" -c '
import json, sys
print(json.dumps(json.load(open(sys.argv[1]))["target"], indent=1))' _work/results/orders/order-0001.json

step "and every result carries it, next to what the runner saw (declared, observed: nothing judges)"
say bx head --json -n 1 '| …'
"$PYTHON" -m benchx.cli head --json -n 1 | "$PYTHON" -c '
import json, sys
doc = json.loads(sys.stdin.readline())
info = doc["provenance"]["info"]
print("provenance.info.how_built  :", info["how_built"].replace("\n", "\n" + " " * 29))
print("provenance.info.activation :", info["activation"])
print("provenance.info.shell      :", info["shell"])
print("provenance.info.target_provider:", info["target_provider"])
print("observed_context.env       :", doc["observed_context"].get("env"))
print("coordinates.subject.configuration:", doc["coordinates"]["subject"].get("configuration"))'

step "forget to activate: the declaration still says what you should have run, the observation says what you did"
say env -u LD_LIBRARY_PATH bx compare @plain @hardened --no-build --rounds 3 ...
env -u LD_LIBRARY_PATH "$PYTHON" -m benchx.cli compare @plain @hardened --no-build --profile environments \
  --label build=plain,hardened --suite demo-bench --rounds 3 --project archery-local \
  --run-key "$RUN-unactivated" --out _work/results-unactivated

printf '\nartifacts: %s/_work (results, orders, build directories, the local store)\n' "$HERE"
