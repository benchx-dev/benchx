# Examples

Each directory here is an example: a `README.md` and the files it uses. CI executes the README
(`tools/execute_markdown.py`) on every pull request, and the documentation site shows the result
(`docs/examples.md` lists the examples).

## Writing one

- `sh` and `bash` fences are executed with Bash (`-e -o pipefail`), a whole fence at a time, in the
  example's directory. `python` fences are executed in one namespace shared by the whole README.
  Add `noexec` after the language (```` ```sh noexec ````) to show a block without running it.
  Unlabelled fences, and any other language (`json`, ...), are shown as they are.
- The first `text` or `json` fence after an executable block is its committed output snapshot. The
  page leaves it out and shows the output of the run instead, in a fence of the same language (so
  `cat benchx.conf.json` followed by a `json` fence shows the file once, as JSON). Prose may come in
  between; a heading or another fence ends the association. The two are never compared. A `json`
  fence that is not the output of the block before it (the file that a command writes, say) needs a
  heading or another fence before it, or another language (`jsonc`).
- A README with no executable block is allowed: it is copied to the site as it is.
- A block that exits non-zero, or a `python` block that raises, fails the example, and with it the
  pull request's check. `bx bench` exits 0 when a benchmark case fails, so a script must assert on
  its results itself, as `pyperf-suite/run.sh` does with its history count.
- A `sh` or `bash` block that is meant to fail, to show an error, says so: ```` ```bash exit=1 ````.
  It must then end with exactly that status: a 0, or any other status, fails the example just as
  an unexpected failure does, so the page cannot go on showing an error the code no longer gives.
  Its output goes into the page as for any block, and the run goes on. The fence is one script
  run with `-e`, so the status is that of the first command that fails, and the commands after it
  do not run: put the failing command last. The page has the fence without `exit=1` (the site's
  fence parser does not take a `key=value` there). Python blocks have no such marker.
- On the page, ANSI escape sequences are removed from the output, and so is the path of the
  repository checkout (`/home/runner/work/benchx/benchx/examples/x` shows as `examples/x`).
- Do not link to other files of the example from the README: the page is built into the site on its
  own, so a link to a file of the example (`bench_suite.py`) is dead there without any warning, and a
  link to a Markdown page that does not exist fails `zensical build --strict`.
- Benchmarks run in the checkout, so the example measures the benchx repository as the tree. Put the
  output in an ignored directory (`out/`, as `pyperf-suite` does).

## Adding one

1. Create `examples/<name>/README.md` and its files.
2. Add `<name>` to the matrix of the `examples` job in `.github/workflows/docs.yml`. Without it
   `tools/gen_docs.py --executed` stops, since the site would show the example unexecuted.
3. Add a line for it under "Executed examples" in `docs/examples.md`.

An example that needs more than `benchx` and `pyperf` (a compiler, another package) gets its own job in
`docs.yml` instead of a matrix leg. The contract is the artifact: the job uploads `docs-<name>`
containing `<name>.md`.

## Running one locally

From the root of the repository, in an environment with benchx and what the example needs
(`uv sync --extra test` gives `bx`, pyperf and the runner's `markdown-it-py`):

```sh
python tools/execute_markdown.py --source examples/pyperf-suite/README.md \
    --output /tmp/pages/pyperf-suite.md --logs /tmp/logs/pyperf-suite
```

The page and the per-block logs go where you point `--output` and `--logs`, not into the checkout.
The example's own output goes to its `out/`. Remove an `out/` that an earlier run left before running
again: in `pyperf-suite` the store rejects a second run under the same run key.

To build the site with the executed pages, in one command (the pages and the logs go in a temporary
directory, and an `out/` left by an earlier run is removed first):

```sh
uv run --python 3.13 --extra parquet --extra test --with-requirements docs/requirements.txt \
    python tools/build_site.py             # into site/; add --serve to preview it
```

Or by hand, after running the examples as above:

```sh
python tools/gen_docs.py --executed /tmp/pages
zensical build --strict --clean    # or: zensical serve
```

Without `--executed`, `tools/gen_docs.py` builds the pages from the committed READMEs, under a
"not executed" notice.
