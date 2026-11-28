"""Build the documentation site with the examples executed, in one command.

    python tools/build_site.py            # into site/
    python tools/build_site.py --serve    # and preview it, http://127.0.0.1:8000

This is what the docs workflow does, on this machine: it executes the README of every example
(tools/execute_markdown.py), puts the pages in docs/generated/examples/ (tools/gen_docs.py --executed), and
builds the site with Zensical in strict mode. The pages and the logs of the runner go in a
temporary directory, which is removed when the site is built; if an example fails, no site is
built and the directory is kept, so that its logs can be read.

The Python that runs this script must have what the examples need and what the build needs:
benchx with `bx` and pyperf (the `parquet` and `test` extras), markdown-it-py (the `test` extra)
and Zensical (docs/requirements.txt). The examples run `bx` and `python` from the PATH, and this
script puts the directory of its own Python first on it. For example:

    uv run --python 3.13 --extra parquet --extra test --with-requirements docs/requirements.txt \\
        python tools/build_site.py

The examples benchmark this checkout, so an uncommitted change to a tracked file shows in their
output as a `dirty-subject-tree` warning. Each example writes into its own `out/`, ignored by git;
one left by an earlier run is removed first, since the stores reject a run that was made before.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

sys.path.insert(0, str(TOOLS))
from gen_docs import example_names  # noqa: E402  # the one definition of what an example is


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the documentation site with the examples executed (see the top of this file)."
    )
    parser.add_argument(
        "--serve", action="store_true", help="preview the site with `zensical serve` instead of building it into site/"
    )
    args = parser.parse_args()

    # The examples run `bx` and `python` from the PATH: the ones of the environment of this Python.
    env = {**os.environ, "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])}
    zensical = shutil.which("zensical", path=env["PATH"])
    problems = []
    if subprocess.run([sys.executable, "-c", "import markdown_it"], capture_output=True, check=False).returncode:
        problems.append("markdown-it-py is not installed in this environment (the `test` extra)")
    if shutil.which("bx", path=env["PATH"]) is None:
        problems.append("`bx` is not on the PATH: benchx is not installed in this environment")
    if zensical is None:
        problems.append("Zensical is not installed in this environment (docs/requirements.txt)")
    if problems:
        return fail("; ".join(problems) + ". See the top of tools/build_site.py.")

    names = example_names()
    tmp = Path(tempfile.mkdtemp(prefix="benchx-examples-"))
    pages, logs = tmp / "pages", tmp / "logs"
    for name in names:
        stale = ROOT / "examples" / name / "out"
        if stale.exists():
            print(f"removing {stale.relative_to(ROOT)}, left by an earlier run", flush=True)
            shutil.rmtree(stale)
        print(f"\n=== executing examples/{name}/README.md", flush=True)
        done = subprocess.run(
            [
                sys.executable,
                str(TOOLS / "execute_markdown.py"),
                "--source",
                f"examples/{name}/README.md",
                "--output",
                str(pages / f"{name}.md"),
                "--logs",
                str(logs / name),
            ],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if done.returncode:
            return fail(f"example {name} failed, so no site was built. Logs, one per block: {logs / name}")

    print("\n=== building the site", flush=True)
    generate = [sys.executable, str(TOOLS / "gen_docs.py"), "--executed", str(pages)]
    if subprocess.run(generate, cwd=ROOT, env=env, check=False).returncode:
        return fail(f"tools/gen_docs.py failed. The pages of the examples are in {pages}")
    shutil.rmtree(tmp)  # the pages are in docs/generated/examples/ now
    build = [zensical, "serve"] if args.serve else [zensical, "build", "--strict", "--clean"]
    try:
        if subprocess.run(build, cwd=ROOT, env=env, check=False).returncode:
            return fail(f"`zensical {build[1]}` failed")
    except KeyboardInterrupt:  # the end of `--serve`
        return 0

    if not args.serve:
        print(
            "\nBuilt site/. To look at it: python -m http.server -d site"
            "  (or run this script with --serve; the pages link by directory, which file:// does not follow)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
