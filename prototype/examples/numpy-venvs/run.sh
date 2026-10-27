#!/usr/bin/env bash
# UC-04 with two venvs: the same code under two numpy versions, measured by separate `bx bench`
# calls that alternate round by round, then compared. See README.md for what each step shows.
#
#   ./run.sh                       numpy 1.26.4 against 2.0.2, 5 rounds
#   ROUNDS=3 ./run.sh              fewer rounds (3 is the fewest that give a verdict)
#   NUMPY_A=2.3.4 NUMPY_B=2.4.3 ./run.sh    other versions (see versions.env)
#
# With `uv`, the venvs use Python $PYTHON (default 3.12: numpy 1.26.4 has no wheels beyond it).
# Without it, they use the python3 that $PYTHON names, which needs wheels for both versions.
# Everything it creates is here: .venv-a, .venv-b and out/ (git-ignored). benchx builds nothing.
set -euo pipefail
cd "$(dirname "$0")"
. ./versions.env
ROUNDS=${ROUNDS:-5}

version() { case $1 in a) echo "$NUMPY_A" ;; b) echo "$NUMPY_B" ;; esac; }

# Run a command with one venv activated, in a subshell.
in_env() { local venv=".venv-$1"; shift
           ( set +u; . "$venv/bin/activate"; set -u; "$@" ); }

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
rm -rf out .venv-a .venv-b; mkdir -p out

say "1. the user prepares both environments; benchx builds nothing"
for side in a b; do
  if command -v uv >/dev/null; then
    uv venv -q --python "${PYTHON:-3.12}" ".venv-$side"
    uv pip install -q --python ".venv-$side/bin/python" "numpy==$(version $side)" pyperf -e ../..
  else
    "${PYTHON:-python3}" -m venv ".venv-$side"
    ".venv-$side/bin/pip" install -q "numpy==$(version $side)" pyperf -e ../..
  fi
  echo "  .venv-$side: numpy $(version $side)"
done

say "2. the caller's loop: alternate the sides, round by round, one slot per call"
S=numpy-$(date +%s); slot=0
for round in $(seq 0 $((ROUNDS - 1))); do
  for side in a b; do
    in_env "$side" bx bench "@$side" \
        --run-key "$S" --label "env=numpy-$(version $side)" \
        --round "$round" --slot "$slot" --out "out/$S" > /dev/null
    echo "  round $round, slot $slot: side $side"
    slot=$((slot + 1))
  done
done

say "3. compare: the sides differ in the numpy the collector read"
in_env a bx compare --run "$S" --profile environments --label env \
    --baseline "numpy-$NUMPY_A" | tee out/compare.txt
grep -q 'differs:.*deps\.numpy' out/compare.txt   # the differing fact is shown
if grep -q 'failed invariants' out/compare.txt; then echo "the comparison was refused" >&2; exit 1; fi

say "4. the check: declare numpy $NUMPY_B for a run in the venv that has $NUMPY_A"
in_env a bx bench @b --run-key "$S-mismatch" --label env=wrong \
    --round 0 --slot 0 --out "out/$S-mismatch" > /dev/null
grep -rq collector-mismatch "out/$S-mismatch"
echo "  the results carry collector-mismatch; the run went ahead, with the numpy it found"
