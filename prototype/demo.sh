#!/usr/bin/env bash
# End-to-end demonstration of prototype-design.md §6: both profiles, both shapes.
#
# The script plays the user, and in that role the target provider: it prepares
# the environment with its own tools, a git repository with two commits and
# three CMake build directories (benchx builds nothing). Then `bx compare` in
# measure mode does the workbench's part (system-decomposition.md §3.4): it
# alternates the sides over the rounds, assigns round and slot, delivers every
# result to the local store, and compares.
#
# Every command is printed before it runs, as you would type it: `bx` stands
# for `$PYTHON -m benchx.cli`, and $WORK for the scratch directory.
#
# Needs cmake, a C++ compiler, and network access the first time, to fetch
# Google Benchmark unless it is installed. ROUNDS=5 by default.
#
# `bx compare` (measure mode) and `bx run` deliver every result to the one local store,
# ~/.benchx/store.parquet (BENCHX_HOME=... relocates it), so the store and
# each series' history grow from run to run. `bx head` shows the latest rows
# at any time.
set -euo pipefail
cd "$(dirname "$0")"
PROTOTYPE=$PWD
PYTHON=${PYTHON:-$PROTOTYPE/.venv/bin/python}
ROUNDS=${ROUNDS:-5}
WORK=$(mktemp -d)
STORE="${BENCHX_HOME:-$HOME/.benchx}/store.parquet"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)  # run keys are unique per demo run, so runs accumulate
RUN_REV="rev-adhoc-$STAMP"
RUN_ENV="env-tracked-$STAMP"
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.org
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.org

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# say <command...>: print a command as you would type it
quote() {
  if [[ $1 =~ ^[A-Za-z0-9_./:=@%+,^-]+$ ]]; then printf '%s' "$1"
  else printf "'%s'" "${1//\'/\'\\\'\'}"; fi
}
SAY_SUFFIX=""
say() {  # to stderr, so a command's own redirected output never contains it
  local line="" arg
  for arg in "$@"; do line+=" $(quote "$arg")"; done
  line="${line# }${SAY_SUFFIX:+ $SAY_SUFFIX}"
  printf '\033[36m$ %s\033[0m\n' "${line//"$WORK"/\$WORK}" >&2
}
show() { say "$@"; "$@"; }                   # print, then run
quiet() { say "$@"; "$@" >/dev/null; }       # print, then run without its output
bx() { say bx "$@"; "$PYTHON" -m benchx.cli "$@"; }
into() {  # into <file> <bx ...>: run with stdout to a file, printed with its redirect
  SAY_SUFFIX="> $1"; local out=$1; shift
  "$@" > "$out"
  SAY_SUFFIX=""
}

printf '\033[36m$ WORK=%s\033[0m\n' "$WORK" >&2

step "user: a source with two commits, a worktree at each"
show cp -R examples/demo-suite "$WORK/src"
show git -C "$WORK/src" init -q
show git -C "$WORK/src" add -A
show git -C "$WORK/src" commit -qm "base"
BASE=$(git -C "$WORK/src" rev-parse HEAD)
show sed -i.bak 's/kWork = 100/kWork = 102/' "$WORK/src/bench.cpp"
rm "$WORK/src/bench.cpp.bak"
show git -C "$WORK/src" commit -qam "head: 2% more work"
HEAD=$(git -C "$WORK/src" rev-parse HEAD)
show git -C "$WORK/src" worktree add -q --detach "$WORK/wt-base" "$BASE"
show git -C "$WORK/src" worktree add -q --detach "$WORK/wt-head" "$HEAD"
echo "base $BASE"
echo "head $HEAD"

step "user: three build directories (base, head, head hardened)"
build() {  # build <worktree> <dir> [cmake args]
  quiet cmake -S "$1" -B "$2" -DCMAKE_BUILD_TYPE=Release -DFETCHCONTENT_BASE_DIR="$WORK/_deps" "${@:3}"
  quiet cmake --build "$2" -j
}
build "$WORK/wt-base" "$WORK/build-base"
build "$WORK/wt-head" "$WORK/build-head"
build "$WORK/wt-head" "$WORK/build-hardened" -DDEMO_HARDENED=ON

step "revisions, ad hoc: no project, measured and compared in one command"
echo "# measure mode: the sides alternate over $ROUNDS rounds, one work order each"
bx compare "$WORK/build-base:$WORK/wt-base" "$WORK/build-head:$WORK/wt-head" --profile revisions \
  --suite demo-bench --rounds "$ROUNDS" --run-key "$RUN_REV" --out "$WORK/results-rev"
echo "# the first work order it wrote:"
show cat "$WORK/results-rev/orders/order-0000.json"
echo
echo "# read mode: the same run key, compared again through a throwaway store"
bx compare --run "$RUN_REV" --profile revisions --baseline "${BASE:0:10}" --results "$WORK/results-rev"
echo "# bx compare also delivered these to the local store, as thin results of project local/<hostname>"

step "environments, tracked: project demo, into the local store"
bx compare "$WORK/build-head:$WORK/wt-head" "$WORK/build-hardened:$WORK/wt-head" --profile environments \
  --label build=plain,hardened --suite demo-bench --rounds "$ROUNDS" --project demo \
  --run-key "$RUN_ENV" --out "$WORK/results-env"

step "re-ingest is a visible no-op"
bx ingest "$WORK/results-env"

step "a conflicting unit is rejected with its taxonomy code"
echo "# \$WORK/bad.json: a copy of one result, its unit changed from s to ms"
"$PYTHON" - "$WORK" <<'EOF'
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
doc = json.loads(next(p for p in sorted((work / "results-env").glob("*.json"))
                      if not p.name.startswith("workorder-")).read_text())
doc["ingest_key"] += ":in-ms"
doc["coordinates"]["quantity"]["unit"] = "ms"
(work / "bad.json").write_text(json.dumps(doc))
EOF
bx ingest "$WORK/bad.json" || true

step "compare from the local store"
bx compare --run "$RUN_ENV" --profile environments --label build --baseline plain

step "the same comparison through a throwaway store gives the same document"
into "$WORK/a.json" bx compare --run "$RUN_ENV" --profile environments --label build --baseline plain --json
into "$WORK/b.json" bx compare --run "$RUN_ENV" --profile environments --label build --baseline plain --json \
  --results "$WORK/results-env"
show cmp "$WORK/a.json" "$WORK/b.json" && echo "identical"

step "series and history (a query; history mode is deferred)"
into "$WORK/series.txt" bx series --workload BM_Quiet
show head -6 "$WORK/series.txt"
SERIES=$(awk '/demo .*est=benchx\/median/ {print $1; exit}' "$WORK/series.txt")
into "$WORK/history.txt" bx history "$SERIES"
show head -1 "$WORK/history.txt"
show tail -n 4 "$WORK/history.txt"

step "the store is one Parquet file, and keeps growing"
show ls -l "$STORE"
bx head -n 8
printf '\ndemo artifacts in %s\n' "$WORK"
