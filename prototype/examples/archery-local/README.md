# archery-local: an end-to-end example

One commit, built two ways (plain and hardened), compared with noise-aware
verdicts, where the comparison says what was built and how. It follows the
Arrow local story, "Measure what a build option costs"
([`docs/user_stories/apache-arrow-local-benchmarking.md`](../../../docs/user_stories/apache-arrow-local-benchmarking.md)),
on a small stand-in suite (`../demo-suite`: a quiet and a noisy Google
Benchmark case, where hardening adds about 3% of work).

It exercises the target-provider contract of
[`system-decomposition.md`](../../../docs/design/system-decomposition.md) §3.4:

- **You are the user, and the provider is your build script.** `provider.sh`
  configures and builds with CMake, as `archery` or a contributor would. benchx
  builds nothing.
- **The provider tells benchx what it built** in a *target description*: the
  build directory and its source, plus two pieces of text you own, `how_built`
  (the exact commands) and `activation` (what to run to enter the environment,
  with the `shell` it is written for). Either may be empty.
- **You activate the environment yourself** (`source activate.sh`). benchx never
  runs `activation`. It records the text, as given, in the work order and in
  every result's `provenance.info`, beside the allowlisted environment variables
  the runner actually inherited. If the two disagree, both stay and nothing
  judges.

## Run it

You need `cmake`, a C++ compiler, `git` and `python3` on your `PATH`, and network
access the first time (CMake fetches Google Benchmark v1.9.1 unless it is
installed). From the repository root:

```console
$ cd prototype
$ uv venv && uv pip install -e '.[test]'      # or any virtualenv with `pip install -e .`
$ cd examples/archery-local
$ ./run.sh
```

It takes about two minutes the first time (two CMake builds and a measured
run) and prints every command before running it, as you would type it. With
another interpreter: `PYTHON=/path/to/python ./run.sh`. More rounds:
`ROUNDS=10 ./run.sh`.

Everything it creates is under `_work/` in this directory, including the local
store (`_work/benchx-home`), so `~/.benchx` is untouched. `rm -rf _work` starts
over.

## What you will see

1. **Activate.** `source activate.sh` puts a stand-in dependencies directory on
   `LD_LIBRARY_PATH`. This is you, not benchx.
2. **`bx target describe @plain`** fails with exit 3 from the provider: nothing is
   built yet. `describe` builds nothing, by contract.
3. **`bx target prepare @plain` / `@hardened`** make the provider build. Its CMake
   output is the build's own, on stderr; the target description it prints on
   stdout is the answer. `how_built` is the very commands the provider ran.
4. **`bx target describe @hardened`** now works. The same document is stored in
   the build directory as `.benchx-target.json`, so a long-lived build directory
   carries its own description (point `bx compare` at a bare directory and it
   reads that file; no provider needed).
5. **`bx compare @plain @hardened ...`** is the workbench's session loop: it
   asks the provider to prepare each side (a no-op build now), then alternates
   the sides over the rounds, one work order each, delivers the results to the
   local store, and compares:

   ```
   run hardening-...  profile environments  baseline plain  contender hardened
     plain: how built: cmake -S .../_work/src -B .../_work/build-plain ... (+1 lines)
     plain: activation: source .../activate.sh
     plain: observed env: LD_LIBRARY_PATH=.../_work/deps/lib
     hardened: how built: cmake -S ... -B .../_work/build-hardened ... -DDEMO_HARDENED=ON (+1 lines)
     ...
   REGRESSED (1)
     BM_Quiet/1024   wall-time  effect +2.42%  noise 0.57%  rounds=5
   NO CHANGE DETECTED (1)
     BM_Noisy/1024   wall-time  effect +3.43%  noise 3.04%  rounds=5
   ```

   Compare that with the story's pains: the comparison now describes itself
   (what was built, how, in what environment), and the verdict uses each
   benchmark's own noise instead of a flat 5%: the quiet case flags a 2.4%
   change that is four times its noise, the noisy case does not flag a 3.4%
   swing inside its noise. The numbers vary from run to run; with few rounds the
   noisy case may flag too.
6. **The work order and a result**, printed: the order's `target` carries
   `how_built`, `activation`, `shell` and `provider`; the result's
   `provenance.info` carries them, next to `observed_context.env` and
   `coordinates.subject.configuration` (read by the runner from
   `CMakeCache.txt`).
7. **Forget to activate.** The same comparison with `LD_LIBRARY_PATH` removed
   from the environment: the declaration still says `source .../activate.sh`,
   the observed environment no longer lists `LD_LIBRARY_PATH`, and nothing
   refuses: declarations are recorded as given (`benchmark-environments.md`
   §3.2), never checked.

## Try it by hand

After `./run.sh` (the build directories and the store stay in `_work/`). From
this directory, with the virtualenv's `bx` on your `PATH` or as
`../../.venv/bin/python -m benchx.cli`:

```console
$ source activate.sh
$ export BENCHX_HOME=$PWD/_work/benchx-home

$ bx target describe @hardened                    # what exists, nothing built
$ bx target prepare  @hardened                    # incremental build, then the description
$ bx target describe _work/build-hardened         # a bare directory: its own sidecar

# the story's own knobs: scope by filter, precision by repetitions
$ bx compare @plain @hardened --profile environments --label build=plain,hardened \
      --suite demo-bench --filter BM_Quiet --rounds 10 --repetitions 5 \
      --project archery-local --run-key mine

$ bx compare --run mine --profile environments --label build --baseline plain   # read mode
$ bx head -n 4                                    # the latest stored results
$ bx head --json -n 1 | python3 -m json.tool | less   # provenance.info, observed_context
```

Edit `provider.sh`'s `hardened)` case or `activate.sh` and run again to see how
each change shows up in the next comparison. Empty text is fine: set
`"activation": ""` (or `null`, or leave it out) in a description and the
runner simply omits it from the results.

## What is in this directory

| File | Role |
|---|---|
| `run.sh` | the end-to-end script |
| `provider.sh` | the target provider: your build script, speaking the `describe`/`prepare` contract |
| `.benchx/provider.json` | tells the workbench which command is the provider |
| `activate.sh` | the environment you activate before calling `bx` |
| `_work/` | everything generated: source copy, build directories, results, store (git-ignored) |

The documents involved are in `../../../schemas/`: `target-provider-request`,
`target-description`, and `work-order` (its `target` takes the declared text).

## What this does not show

The provider here builds one source tree, `WORKSPACE`. Comparing against a tag
or a past revision (`@plain=v1.0`) is the provider's business and is not
implemented in this example; the prototype's own `demo.sh` shows revisions
with two prebuilt worktrees. A declared `build` object in a description is
refused by the prototype's runner (it reads `CMakeCache.txt` instead).
