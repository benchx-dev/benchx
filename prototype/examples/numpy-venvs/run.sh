#!/usr/bin/env bash
# UC-04 with two venvs: the same code under two numpy versions, measured by separate `bx bench`
# calls that alternate round by round, then compared. See README.md for what each step shows.
#
# Needs `uv`. Everything it creates is here: .venv-a, .venv-b and out/ (git-ignored).
# benchx builds nothing.
set -euo pipefail
cd "$(dirname "$0")"

# provider.sh reads the numpy versions from the environment. numpy 1.26.4 has no wheels beyond
# Python 3.12, so both venvs use it. Three rounds are the fewest that give a verdict.
export NUMPY_A=1.26.4 NUMPY_B=2.0.2
PYTHON_VERSION=3.12
ROUNDS=3
SIDES=("a $NUMPY_A" "b $NUMPY_B")
RUN_KEY=numpy-$(date +%s)
SLOT=0

rm -rf out .venv-a .venv-b
mkdir -p out

echo "== 1. the user prepares both environments; benchx builds nothing"
for SIDE_VERSION in "${SIDES[@]}"; do
  read -r SIDE VERSION <<<"$SIDE_VERSION"
  uv venv -q --python "$PYTHON_VERSION" ".venv-$SIDE"
  # benchx imports pyarrow, and pyarrow 25 refuses to import under numpy 1.x.
  uv pip install -q --python ".venv-$SIDE/bin/python" "numpy==$VERSION" "pyarrow<25" pyperf -e ../..
  echo "  .venv-$SIDE: numpy $VERSION"
done

echo "== 2. the caller's loop: alternate the sides, round by round, one slot per call"
for ROUND in $(seq 0 $((ROUNDS - 1))); do
  for SIDE_VERSION in "${SIDES[@]}"; do
    read -r SIDE VERSION <<<"$SIDE_VERSION"
    # Activating a venv replaces the one activated before; no deactivate is needed.
    source ".venv-$SIDE/bin/activate"
    bx bench "@$SIDE" --run-key "$RUN_KEY" --label "env=numpy-$VERSION" \
        --round "$ROUND" --slot "$SLOT" --out "out/$RUN_KEY" > /dev/null
    echo "  round $ROUND, slot $SLOT: side $SIDE"
    SLOT=$((SLOT + 1))
  done
done

echo "== 3. compare: the sides differ in the numpy the collector read"
bx compare --run "$RUN_KEY" --profile environments --label env \
    --baseline "numpy-$NUMPY_A" | tee out/compare.txt
grep -q 'differs:.*deps\.numpy' out/compare.txt   # the differing fact is shown
if grep -q 'failed invariants' out/compare.txt; then echo "the comparison was refused" >&2; exit 1; fi

echo "== 4. the check: declare numpy $NUMPY_B for a run in the venv that has $NUMPY_A"
source .venv-a/bin/activate
bx bench @b --run-key "$RUN_KEY-mismatch" --label env=wrong \
    --round 0 --slot 0 --out "out/$RUN_KEY-mismatch" > /dev/null
grep -rq collector-mismatch "out/$RUN_KEY-mismatch"
echo "  the results carry collector-mismatch; the run went ahead, with the numpy it found"
