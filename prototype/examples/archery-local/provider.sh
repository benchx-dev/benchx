#!/usr/bin/env bash
# The target provider of this example (system-decomposition.md §3.4).
#
# It is the user's own build script, speaking the provider contract: one JSON
# request on stdin (schemas/target-provider-request/0.1.0), one target
# description on stdout (schemas/target-description/0.1.0). Everything else,
# above all the build's own output, goes to stderr.
#
#   describe  reports a target that exists and builds nothing; exit 3 if none
#   prepare   configures and builds it, with CMake, as Arrow's contributors do
#             (an incremental no-op when it is already built), then reports it
#
# Configurations: `plain`, and `hardened` (-DDEMO_HARDENED=ON), the same
# commit built two ways, like the -DARROW_HARDENING=ON story.
#
# `how_built` is assembled from the very commands this script runs, so the
# text cannot drift from what happened. benchx never runs any of it.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${BENCHX_EXAMPLE_WORK:-$HERE/_work}
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.org
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.org

request=$(cat)
field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1]) or "")' "$1" <<<"$request"; }
operation=$(field operation)
configuration=$(field configuration)
case "$configuration" in
  plain)    extra=() ;;
  hardened) extra=(-DDEMO_HARDENED=ON) ;;
  *) echo "provider: unknown configuration '$configuration' (plain or hardened)" >&2; exit 2 ;;
esac
SRC=$WORK/src
BUILD=$WORK/build-$configuration

describe() {
  [[ -f $BUILD/.benchx-target.json ]] || { echo "provider: $configuration is not built; use prepare" >&2; exit 3; }
  cat "$BUILD/.benchx-target.json"
}

if [[ $operation == describe ]]; then describe; exit 0; fi

HOW=""
step() {  # step <command...>: record it in how_built, show it, run it (output to stderr)
  local line="$*"
  HOW+="$line"$'\n'
  printf '\033[2m  provider$ %s\033[0m\n' "$line" >&2
  "$@" >&2
}

if [[ ! -d $SRC/.git ]]; then
  mkdir -p "$WORK"
  cp -R "$HERE/../demo-suite" "$SRC"
  git -C "$SRC" init -q
  git -C "$SRC" remote add origin https://example.org/archery-local/demo-suite.git
  git -C "$SRC" add -A
  git -C "$SRC" commit -qm "demo-suite"
fi
mkdir -p "$WORK/deps/lib"   # stand-in for the dependencies activate.sh points at

step cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DFETCHCONTENT_BASE_DIR="$WORK/_deps" "${extra[@]}"
step cmake --build "$BUILD" -j

BUILD=$BUILD SRC=$SRC HOW=$HOW CONFIGURATION=$configuration HERE=$HERE python3 - >"$BUILD/.benchx-target.json" <<'PY'
import json, os
print(json.dumps({
    "schema_version": "benchx/target-description/0.1.0",
    "target": {"kind": "build", "path": os.environ["BUILD"],
               "source": {"uri": "https://example.org/archery-local/demo-suite.git", "type": "git"},
               "source_dir": os.environ["SRC"]},
    "how_built": os.environ["HOW"].rstrip("\n"),
    "activation": "source " + os.path.join(os.environ["HERE"], "activate.sh"),
    "shell": "bash",
    "provider": {"name": "provider.sh", "version": "1"},
}, indent=1))
PY
describe   # the sidecar just written, which stays with the build directory
