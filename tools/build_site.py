"""Build the documentation site locally, with the pages of the examples, in one command.

    python tools/build_site.py [PATH] [-e NAME ...]

PATH is a checkout of benchx_examples that has scripts/execute_markdown.py (it is on the branch
of its pull request #16, not yet on its main). The script

  1. runs the README of each example with that runner: the examples listed in
     PATH/.github/workflows/e2e.yml (the ones CI runs), or those named with -e. This takes
     about as long as the examples do, and the numbers are those of this machine;
  2. puts the pages where the site expects them, docs/examples/<name>/<name>.md;
  3. runs tools/gen_docs.py, and `zensical build --strict --clean`, as the docs workflow does.

The HTML is in site/. Open site/index.html, or serve it: `python -m http.server -d site`.

It builds the site also when there are no examples: without PATH, or when none is found or can
be run, the Examples page says that none was built, and a message says why, before the build
and again at the end. An example that fails is left out in the same way, with an error message.

docs/examples/ is generated (and gitignored), and it is emptied first, so that the site shows
what this run made and nothing left over from an earlier one.

The Python that runs this script must have what the examples and the build need: benchx,
pyperf and markdown-it-py (the runner is run with this Python, and the examples run `bx` and
`python` from the PATH), and Zensical (`pip install -r docs/requirements.txt`).

Exit status: 0 if the site was built, with or without examples; 1 if the build failed, or if
an example failed to run (the site is then still built, without that example).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "docs" / "examples"
LOGS = ROOT / ".cache" / "examples-logs"  # .cache/ is gitignored
NAME = re.compile(r"[A-Za-z0-9_-]+")


def zensical() -> str | None:
    """The zensical of this Python's environment, else the one on the PATH."""
    beside = Path(sys.executable).parent / "zensical"
    return str(beside) if beside.is_file() else shutil.which("zensical")


def examples_run_by_ci(checkout: Path, notes: list[str]) -> list[str]:
    """The `example:` values in the checkout's e2e workflow, in order."""
    workflow = checkout / ".github" / "workflows" / "e2e.yml"
    if not workflow.is_file():
        notes.append(f"{workflow} not found, so there is no list of the examples that CI runs "
                     "(name them with -e NAME)")
        return []
    names = re.findall(r"^\s+example:\s*([A-Za-z0-9_-]+)\s*$", workflow.read_text(), re.M)
    if not names:
        notes.append(f"no `example:` in {workflow} (name the examples with -e NAME)")
    return list(dict.fromkeys(names))


def run_examples(checkout: Path, wanted: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Run the examples of the checkout. Returns (names made, notes, failures)."""
    made: list[str] = []
    notes: list[str] = []
    failures: list[str] = []
    runner = checkout / "scripts" / "execute_markdown.py"
    if not runner.is_file():
        notes.append(f"{runner} not found: it is on the branch of benchx_examples pull request #16, "
                     "not yet on main")
        return made, notes, failures
    names = wanted or examples_run_by_ci(checkout, notes)
    if not names:
        return made, notes, failures
    if subprocess.run([sys.executable, "-c", "import markdown_it"], capture_output=True).returncode:
        failures.append("markdown-it-py is not installed in this environment, so the examples "
                        "cannot be run (pip install markdown-it-py pyperf benchx)")
        return made, notes, failures
    if shutil.which("bx") is None:
        notes.append("`bx` is not on the PATH: the examples will probably fail "
                     "(pip install benchx, or pip install -e . in this repository)")
    for name in names:
        source = checkout / "examples" / name / "README.md"
        if not source.is_file():
            notes.append(f"{source} not found")
            continue
        print(f"\n=== running examples/{name}/README.md", flush=True)
        result = subprocess.run(
            [sys.executable, str(runner), "--source", str(source),
             "--output", str(EXAMPLES / name / f"{name}.md"), "--logs", str(LOGS / name)],
            cwd=checkout,
        )
        if result.returncode or not (EXAMPLES / name / f"{name}.md").is_file():
            shutil.rmtree(EXAMPLES / name, ignore_errors=True)
            failures.append(f"example {name} failed (logs in {LOGS / name})")
        else:
            made.append(name)
    return made, notes, failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the documentation site locally, with the pages of the examples "
                    "(see the top of this file).")
    parser.add_argument("checkout", nargs="?", type=Path,
                        help="a benchx_examples checkout, whose examples are run; without it the site "
                             "is built with no examples")
    parser.add_argument("-e", "--example", action="append", default=[], metavar="NAME",
                        help="run only this example (repeatable); default: those CI runs")
    args = parser.parse_args()
    bad = [n for n in args.example if not NAME.fullmatch(n)]
    if bad:
        parser.error(f"not an example name: {bad[0]!r}")
    if args.checkout is not None and not args.checkout.is_dir():
        parser.error(f"{args.checkout} is not a directory")
    build = zensical()
    if build is None:
        print("error: zensical is not installed in this environment: pip install -r docs/requirements.txt",
              file=sys.stderr)
        return 1

    if EXAMPLES.exists():
        shutil.rmtree(EXAMPLES)
    if args.checkout is None:
        made, notes, failures = [], ["no benchx_examples checkout was given"], []
    else:
        made, notes, failures = run_examples(args.checkout.resolve(), args.example)

    def report(verb: str) -> None:
        """The messages about the examples: before the build, and again at its end."""
        for note in notes:
            print(f"note: {note}", file=sys.stderr, flush=True)
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr, flush=True)
        if made:
            print(f"{verb} with {len(made)} example(s): {', '.join(made)}", flush=True)
        else:
            print(f"{verb} WITHOUT examples: the Examples page will say that none was built.",
                  file=sys.stderr, flush=True)

    print("\n=== building the site", flush=True)
    report("Building the site")
    status = subprocess.run([sys.executable, str(ROOT / "tools" / "gen_docs.py")], cwd=ROOT).returncode
    if not status:
        status = subprocess.run([build, "build", "--strict", "--clean"], cwd=ROOT).returncode
    if status:
        print("error: the site was not built (see above)", file=sys.stderr)
        return 1
    print()
    report("Built site/")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
