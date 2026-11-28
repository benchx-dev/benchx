"""Execute the code blocks of a Markdown file and render a page with fresh output.

    python tools/execute_markdown.py --source examples/NAME/README.md \\
        --output OUT/NAME.md --logs LOGS/NAME

Run it from the root of the repository: the path of that directory is what is left out of
the output (see below), and what GitHub annotations are relative to. It needs `markdown-it-py`
(the `test` extra of benchx).

Conventions:
- sh/bash fences: executed with Bash (-e -o pipefail), a whole fence at a time, in the
  directory of the source file. A `$ ` at the start of a line is removed before the run (the
  page keeps it, as a prompt).
- python fences: executed in one namespace shared by the whole file.
- A language followed by `noexec` (```sh noexec) is not executed.
- A sh/bash fence that is meant to fail says so: ```sh exit=1. The block must end with exactly
  that status (a 0 fails the run as well as any other status), its output goes into the page as
  for any block, and the run goes on. The fence is one script run with -e, so the status is that
  of the first command that fails and the commands after it do not run: put it last. The page
  has the fence without its `exit=1`: the site's fence parser does not take a key=value there.
  Python fences have no such marker: one that raises fails the run.
- A sh/bash fence that ends with status 0 must also leave no failed result in the example's
  store: `bx bench` exits 0 when a case fails, but it stores the result with its status. The
  runner reads the store (`bx head --json`, run in the directory of the source file) before and
  after the block, and a result that is new and has the status error, censored, partial or
  skipped fails the run. The load-average results are not the benchmark's own and are left out.
  A block marked `exit=N` with N other than 0 is not checked: it is meant to fail. Nothing is
  checked where `bx` is not on the PATH. Only what is stored is seen: a run with --no-ingest, or
  a store that the fence itself relocates (with BENCHX_HOME, say), is not.
- The first `text` or `json` fence after an executable block is a committed output snapshot:
  it is left out of the page, and the output of the run is put after the block instead, in a
  fence of the same language (so the output of `cat config.json` is shown as JSON, once). Prose
  may come between the block and its snapshot; a heading or another fence ends the association.
  A `json` fence that is not the output of the block before it (the file that a command writes,
  say) needs a heading or another fence in between, or another language (`jsonc`).
- Unlabelled and other fences are kept as they are.
- Output is never compared with the snapshot.
- A file with no executable block is allowed: the page is a copy of it.

In the output that goes into the page, ANSI escape sequences are removed and the path of the
directory the runner was started in is dropped (`/home/runner/work/x/x/examples/a/b` is
printed as `examples/a/b`, the directory itself as `.`). The logs keep the output as it was.

The source file is never modified. The page and the logs are written to other places, and
the page only after every block has run: a failing block leaves no page.
"""

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from markdown_it import MarkdownIt

EXECUTABLE_LANGUAGES = {"sh", "bash", "python"}

# The languages of a committed output snapshot, which the fresh output replaces.
SNAPSHOT_LANGUAGES = {"text", "json"}

# The statuses of a failed result. `skipped` is one too: an example runs where what it needs is
# installed, so a skipped case means that something is missing.
FAILED_STATUSES = {"error", "censored", "partial", "skipped"}

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean(text: str, root: Path) -> str:
    """Output as it goes into the page: no ANSI escapes, no path of the runner's checkout."""
    text = ANSI.sub("", text)
    prefix = str(root)

    if len(prefix) > 1:
        text = text.replace(prefix + os.sep, "").replace(prefix, ".")

    return text


def text_fence(text: str, language: str = "text") -> str:
    """Format captured output as a Markdown fence.

    `text`, unless it replaces a committed snapshot of another language (`json`).
    """
    if not text.strip():
        return ""

    # Use a fence longer than any backtick sequence in output.
    runs = re.findall(r"`+", text)
    marker = "`" * max(3, max(map(len, runs), default=0) + 1)

    return f"\n{marker}{language}\n{text.rstrip()}\n{marker}\n"


def expected_status(attributes: list[str], language: str) -> int:
    """The exit status a block must end with: N for `exit=N` after the language, else 0."""
    given = [a for a in attributes if a.startswith("exit=")]
    if not given:
        return 0

    if language not in {"sh", "bash"}:
        raise ValueError(f"{given[0]}: only sh and bash blocks take exit=; a {language} block fails by raising")

    if len(given) > 1:
        raise ValueError(f"{' '.join(given)}: a block has one exit=")

    value = given[0].removeprefix("exit=")

    if not re.fullmatch(r"[0-9]{1,3}", value) or int(value) > 255:
        raise ValueError(f"{given[0]}: exit= takes a status from 0 to 255")

    return int(value)


def page_fence(fence_lines: list[str], token) -> list[str]:
    """The lines of a fence as they go into the page: without its `exit=N`.

    The site's fence parser (pymdownx.superfences) does not accept an unquoted key=value after the
    language: it shows the fence as text, with the markup that follows it. The README keeps
    `exit=N`, since that is what the runner reads; the page has no use for it.
    """
    parts = token.info.strip().split()
    kept = [part for part in parts if not part.startswith("exit=")]

    if len(kept) == len(parts):
        return fence_lines

    opening = fence_lines[0]
    indent = opening[: len(opening) - len(opening.lstrip(" "))]
    newline = opening[len(opening.rstrip("\r\n")) :]

    return [f"{indent}{token.markup}{' '.join(kept)}{newline}", *fence_lines[1:]]


def without_prompts(code: str) -> str:
    """The code of a Bash block with the `$ ` prompt removed from the start of its lines."""
    return re.sub(r"(?m)^\$ ", "", code)


def failed_results(workdir: Path) -> dict[str, str] | None:
    """The failed results in the store of the example in `workdir`: what each says, by ingest key.

    None where `bx` is not installed. `bx bench` exits 0 when a case fails, but it stores the
    result with its status. The load-average results (a condition measurement, skipped where the
    platform has no probe) are not the benchmark's own and are left out.
    """
    if shutil.which("bx") is None:
        return None

    done = subprocess.run(
        ["bx", "head", "-n", str(10**9), "--json"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )

    if done.returncode:
        raise RuntimeError(
            f"bx head could not read the store (status {done.returncode}): {(done.stderr or done.stdout).strip()}"
        )

    failed = {}

    for line in done.stdout.splitlines():
        result = json.loads(line)
        measurement = result["measurement"]

        if measurement["status"] not in FAILED_STATUSES or measurement.get("kind") == "condition":
            continue

        workload = result["coordinates"]["workload"]
        parameters = ", ".join(f"{k}={v}" for k, v in (workload.get("parameters") or {}).items())
        case = workload["name"] + (f" [{parameters}]" if parameters else "")
        message = (result["provenance"].get("info") or {}).get("message")
        said = (
            f"{measurement['status']} ({measurement['reason']})" if "reason" in measurement else measurement["status"]
        )

        failed[result["ingest_key"]] = (
            f"{result['provenance']['run_key']}: {case}, {measurement['quantity']['name']}: {said}"
            + (f": {message}" if message else "")
        )

    return failed


def execute_bash(
    code: str,
    workdir: Path,
    logfile: Path,
    expected: int = 0,
) -> str:
    """Execute a complete Bash block and stream output to logs.

    The block must end with the status `expected`: any other one raises. A block that ends with 0
    must not have added a failed result to the store either (see `failed_results`).
    """
    chunks = []
    before = failed_results(workdir) if expected == 0 else None

    with (
        logfile.open("w", encoding="utf-8") as log,
        subprocess.Popen(
            ["bash", "-e", "-o", "pipefail", "-c", code],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as process,
    ):
        assert process.stdout is not None

        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
            chunks.append(line)

        status = process.wait()

    if status != expected:
        marked = f", not the exit={expected} of the block" if expected else ""
        raise RuntimeError(f"Bash exited with status {status}{marked}; see {logfile}")

    if expected:
        print(f"[exit status {status}, as the block says]", flush=True)

    if before is not None:
        added = {key: said for key, said in failed_results(workdir).items() if key not in before}

        if added:
            raise RuntimeError(
                f"Bash exited with status 0, but {len(added)} result(s) it stored failed:\n  "
                + "\n  ".join(added.values())
            )

    return "".join(chunks)


def execute_python(
    code: str,
    namespace: dict,
    logfile: Path,
    filename: str,
) -> str:
    """Execute Python with shared state and capture diagnostics."""
    captured = io.StringIO()

    try:
        with (
            contextlib.redirect_stdout(captured),
            contextlib.redirect_stderr(captured),
        ):
            # Running the README's code is the point of this tool.
            exec(compile(code, filename, "exec"), namespace)  # noqa: S102

    except BaseException:
        captured.write(traceback.format_exc())
        output = captured.getvalue()

        logfile.write_text(output, encoding="utf-8")
        print(output, file=sys.stderr, flush=True)
        raise

    output = captured.getvalue()
    logfile.write_text(output, encoding="utf-8")

    if output:
        print(output, end="", flush=True)

    return output


def github_annotation(
    source: Path,
    line: int,
    message: str,
) -> None:
    """Emit a GitHub Actions error annotation for a source line."""

    def escape(value: str, property: bool = False) -> str:
        value = str(value).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

        if property:
            value = value.replace(":", "%3A").replace(",", "%2C")

        return value

    print(
        f"::error file={escape(source, True)},line={line}::{escape(message)}",
        flush=True,
    )


def run(
    source: Path,
    output: Path,
    logs: Path,
) -> int:
    """Execute a Markdown file and render fresh output.

    Returns the number of executable blocks that were run. A failing block raises.
    """
    repository_root = Path.cwd().resolve()

    source = source.resolve()
    output = output.resolve()
    logs = logs.resolve()

    if source == output:
        raise ValueError("Output must differ from source")

    output.parent.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    # Execute in the original example directory.
    # BenchX needs the real Git checkout to record revision and
    # dirty-tree information correctly.
    workdir = source.parent

    original = source.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    tokens = MarkdownIt("commonmark").parse(original)

    # markdown-it-py identifies fences and headings.
    # Do not filter on token.level: valid fenced blocks may
    # have a nonzero nesting level.
    events = [token for token in tokens if token.map is not None and token.type in {"fence", "heading_open"}]

    rendered = []
    cursor = 0
    awaiting_output = False
    fresh_output = None  # (index in `rendered`, cleaned text) of the last block's output
    block_number = 0

    # Python blocks share variables across the document.
    namespace = {"__name__": "__main__"}

    previous_cwd = Path.cwd()

    try:
        os.chdir(workdir)

        for token in events:
            start, end = token.map

            # Preserve all ordinary Markdown between events.
            rendered.extend(lines[cursor:start])

            if token.type == "heading_open":
                awaiting_output = False
                rendered.extend(lines[start:end])

            elif token.type == "fence":
                parts = token.info.strip().split()
                language = parts[0] if parts else ""

                executable = language in EXECUTABLE_LANGUAGES and "noexec" not in parts[1:]

                if language in SNAPSHOT_LANGUAGES and awaiting_output:
                    # Remove the first committed text or json output
                    # snapshot after an executable block.
                    #
                    # Ordinary prose may intervene, but a heading
                    # or another fence ends the association.
                    #
                    # Fresh output has already been inserted
                    # immediately after the executable block, as
                    # text: it takes the language of the snapshot.
                    #
                    # We never compare committed and fresh output.
                    if language != "text":
                        index, shown = fresh_output
                        rendered[index] = text_fence(shown, language)

                    awaiting_output = False

                elif executable:
                    block_number += 1
                    line = start + 1
                    logfile = logs / f"block-{block_number:03d}.log"

                    # Preserve original code fence in rendered docs.
                    rendered.extend(page_fence(lines[start:end], token))

                    print(
                        f"\n[{block_number}] {source}:{line} ({' '.join(parts)})",
                        flush=True,
                    )

                    try:
                        # An invalid exit= is an error of the README: before anything runs.
                        expected = expected_status(parts[1:], language)

                        if language in {"sh", "bash"}:
                            result = execute_bash(
                                without_prompts(token.content),
                                workdir,
                                logfile,
                                expected,
                            )
                        else:
                            result = execute_python(
                                token.content,
                                namespace,
                                logfile,
                                f"{source}:{line}",
                            )

                    except BaseException as exc:
                        try:
                            relative_source = source.relative_to(repository_root)
                        except ValueError:
                            relative_source = source

                        github_annotation(
                            relative_source,
                            line,
                            f"{language} block failed: {exc}",
                        )

                        print(
                            f"Failed block {block_number}." + (f" Diagnostics: {logfile}" if logfile.exists() else ""),
                            file=sys.stderr,
                        )
                        raise

                    # Insert fresh text output after executable code.
                    # Empty output does not produce a text fence.
                    shown = clean(result, repository_root)
                    rendered.append(text_fence(shown))
                    fresh_output = (len(rendered) - 1, shown)

                    awaiting_output = True

                else:
                    # Preserve unlabelled, json, yaml, noexec,
                    # and other non-executable fences unchanged.
                    rendered.extend(page_fence(lines[start:end], token))
                    awaiting_output = False

            cursor = end

        rendered.extend(lines[cursor:])

        if block_number == 0:
            print(
                f"No executable blocks in {source}: copied as it is.",
                flush=True,
            )

        # Write generated Markdown only after successful execution.
        output.write_text(
            "".join(rendered),
            encoding="utf-8",
        )

    finally:
        os.chdir(previous_cwd)

    print(f"\nGenerated documentation: {output}")

    return block_number


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute Markdown examples and capture outputs")

    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Source Markdown file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Generated Markdown file",
    )
    parser.add_argument(
        "--logs",
        type=Path,
        required=True,
        help="Directory for per-block diagnostic logs",
    )

    args = parser.parse_args()
    run(args.source, args.output, args.logs)


if __name__ == "__main__":
    main()
