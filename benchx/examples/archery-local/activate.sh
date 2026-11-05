# The environment this example's benchmarks are meant to run in. In the Arrow
# story this is where you would load a toolchain or point at the dependencies
# you built; here it only puts a stand-in "deps" directory on the library
# search path. You source it yourself, before invoking bx: benchx never runs
# it, it records the text that says you did (the target description's
# `activation`) next to the environment it actually inherited.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export LD_LIBRARY_PATH="$HERE/_work/deps/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
