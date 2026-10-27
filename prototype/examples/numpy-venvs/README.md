# numpy-venvs: UC-04 with two virtual environments

The same code, measured under two numpy versions, by separate `bx bench` calls that alternate
round by round, then compared. This is
[UC-04](../../../docs/use-cases/UC-04-cross-env.md), "fixed branch, comparing environments":
`numpy==1.26.4` and `numpy==2.0.2` cannot be imported in one process, so each side runs in its
own environment, and a shell script drives the loop.

You prepare the environments and enter them; benchx builds nothing and activates nothing.

## Run it

You need [`uv`](https://docs.astral.sh/uv/) (it fetches Python 3.12, the newest Python with numpy
1.26.4 wheels) and network access the first time. Then, from this directory:

```console
$ ./run.sh
```

The versions (`NUMPY_A`, `NUMPY_B`), the Python and the number of rounds are set at the top of
`run.sh`. Everything it creates is in `.venv-a`, `.venv-b` and `out/`, all git-ignored.

## What it does

1. **Prepares two venvs**, `.venv-a` and `.venv-b`, each with its numpy, pyperf and benchx, and
   `pyarrow<25`: benchx imports pyarrow, and pyarrow 25 refuses to import under numpy 1.x.
2. **Runs the caller's loop.** For each of the 3 rounds, one `bx bench` call per side, each in its
   venv (`run.sh` sources the venv's `activate`; activating the other venv replaces it, so nothing
   needs deactivating). The calls share a `--run-key`; each says which side it is
   (`--label env=numpy-1.26.4`), which round, and which slot of the alternation.
3. **Compares.** `bx compare --run S --profile environments --label env --baseline numpy-1.26.4`.
   The sides differ because the collector read a different numpy in each. The comparison says
   what differed (`differs: ...deps.numpy`), shows what each side declared, and gives a verdict per
   benchmark:

   ```
   run numpy-1793121294  profile environments  baseline numpy-2.3.4  contender numpy-2.4.3
     numpy-2.3.4: how built: {"summary":{"numpy":"2.3.4"}}
     numpy-2.4.3: how built: {"summary":{"numpy":"2.4.3"}}
     differs: coordinates.environment.system.deps.numpy, coordinates.environment.system.deps.sha256

   NO CHANGE DETECTED (3)
     bench_matmul   wall-time  effect  -16.28%  noise   7.43%  rounds=3
     ...
   ```

   (The numbers vary from run to run; `[local-only]` appears while the demo's files are
   uncommitted.)
4. **Checks a declaration.** It declares numpy 2.0.2 for a run in the venv that has 1.26.4. The
   collector disagrees, so every result of that run carries `collector-mismatch`. The run goes
   ahead, because the collector has `on_fail: warn`, and the recorded numpy is the one found.

## How the sides are told apart

- `describe_env.py` is a **context collector** (`harness-adapter.md` §5.1), named in
  `benchx.conf.json`. benchx runs it once per call, in the environment the call runs in. It prints
  `{"summary": {"numpy": "<version>"}}`, and that lands in each result's `environment.system.deps`,
  which the `environments` comparison treats as the varying coordinate. benchx knows nothing about
  venvs, conda or pixi: any command that prints facts about an environment does the same.
- `provider.sh` is the **target provider**. For configuration `a` or `b` it declares, in
  `how_built`, the numpy that side is meant to have. The collector has `verify: how_built`, so it
  compares the declaration with what it found. The declared object has the same shape as the
  collector's output, because the check compares key by key from the top.
- A check that fails refuses the run by default; `on_fail: warn` records a warning instead, which
  is what the design asks for (a mismatch is recorded, not a gate).

## UC-04's spellings, and the prototype's

[UC-04](../../../docs/use-cases/UC-04-cross-env.md) is kept as written, and uses spellings the
prototype does not have. This is how each corresponds:

- `benchx new-session` (`SESSION=$(...)`) → no command. The session is the run key, chosen by the
  caller: `S=numpy-$(date +%s)`.
- `--session-id "$SESSION"` → `--run-key "$S"`. It lands in `provenance.run_key`.
- `benchx run run_benchmark.py` → `bx bench`. With a module and functions,
  `bx bench MODULE.py [FUNCTION ...]`; with a prepared target, `bx bench @CONFIG --suite NAME`.
  `bx run` takes an order file, not a script.
- `--env-label numpy-1.26.4` → `--label env=numpy-1.26.4`. The field `env_label` is
  `provenance.labels.env`.
- `--round "$round"` → `--round N` and `--slot K`. UC-04 uses one number for two things. The
  prototype separates the round, which pairs one attempt of each side (`procedure.round`), from
  the slot, the realized position in the interleaved sequence (`procedure.slot`). UC-04's
  `interleave_position` is the slot. The caller's loop keeps the slot counter.
- `benchx compare-environments --session-id S --baseline A --variant B` →
  `bx compare --run S --profile environments --label env --baseline A`. The profile is UC-04's
  `cross-environment-comparison`. There are exactly two sides, so the variant is the other one.
- `.venv-baseline/bin/benchx` → the `bx` of that environment, such as `.venv-a/bin/bx`.
- The output table (`benchmark_name`, `env`, `time`, `vs. baseline`) → the comparison output:
  verdict groups with the effect, the noise and the rounds, and the facts that differ.
- UC-04's `session_id`, `env_label` and `interleave_position` fields → `provenance.run_key`,
  `provenance.labels.env` and `procedure.slot`. The schema adds no fields for a profile
  (`benchmark-result-schema.md` §5.5).

## What is in this directory

| File | Role |
|---|---|
| `run.sh` | the driver: venvs, the loop, the comparison, the check |
| `benchx.conf.json` | the project file: the adapter and suite, the provider, the `deps` collector |
| `provider.sh` | the target provider: declares each side's numpy in `how_built` (`run.sh` exports the versions) |
| `describe_env.py` | the collector: prints the numpy this Python imports |
| `bench_suite.py` | three small numpy benchmarks |
