"""Execute the code blocks of a Markdown file and render a page with fresh output.

    python tools/execute_markdown.py --source examples/NAME/README.md \\
        --output OUT/NAME.md --logs LOGS/NAME

Run it from the root of the repository: the path of that directory is what is left out of
the output (see below), and what GitHub annotations are relative to. It needs `markdown-it-py`
(the `test` extra of benchx).

Conventions:
- sh/bash fences: executed with Bash (-e -o pipefail), a whole fence at a time, in the
  directory of the source file.
- python fences: executed in one namespace shared by the whole file.
- A language followed by `noexec` (```sh noexec) is not executed.
- The first `text` fence after an executable block is a committed output snapshot: it is
  left out of the page, and the output of the run is put after the block instead. Prose may
  come between the block and its snapshot; a heading or another fence ends the association.
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
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

from markdown_it import MarkdownIt

EXECUTABLE_LANGUAGES = {"sh", "bash", "python"}

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean(text: str, root: Path) -> str:
    """Output as it goes into the page: no ANSI escapes, no path of the runner's checkout."""
    text = ANSI.sub("", text)
    prefix = str(root)

    if len(prefix) > 1:
        text = text.replace(prefix + os.sep, "").replace(prefix, ".")

    return text


def text_fence(text: str) -> str:
    """Format captured output as a Markdown text fence."""
    if not text.strip():
        return ""

    # Use a fence longer than any backtick sequence in output.
    runs = re.findall(r"`+", text)
    marker = "`" * max(3, max(map(len, runs), default=0) + 1)

    return f"\n{marker}text\n{text.rstrip()}\n{marker}\n"


def execute_bash(
    code: str,
    workdir: Path,
    logfile: Path,
) -> str:
    """Execute a complete Bash block and stream output to logs."""
    chunks = []

    with logfile.open("w", encoding="utf-8") as log, subprocess.Popen(
        ["bash", "-e", "-o", "pipefail", "-c", code],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as process:
        assert process.stdout is not None

        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
            chunks.append(line)

        status = process.wait()

    if status != 0:
        raise RuntimeError(
            f"Bash exited with status {status}; see {logfile}"
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
        value = (
            str(value)
            .replace("%", "%25")
            .replace("\r", "%0D")
            .replace("\n", "%0A")
        )

        if property:
            value = (
                value.replace(":", "%3A")
                .replace(",", "%2C")
            )

        return value

    print(
        f"::error file={escape(source, True)},"
        f"line={line}::{escape(message)}",
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
    events = [
        token
        for token in tokens
        if token.map is not None
        and token.type in {"fence", "heading_open"}
    ]

    rendered = []
    cursor = 0
    awaiting_output = False
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

                executable = (
                    language in EXECUTABLE_LANGUAGES
                    and "noexec" not in parts[1:]
                )

                if language == "text" and awaiting_output:
                    # Remove the first committed text-output
                    # snapshot after an executable block.
                    #
                    # Ordinary prose may intervene, but a heading
                    # or another fence ends the association.
                    #
                    # Fresh output has already been inserted
                    # immediately after the executable block.
                    #
                    # We never compare committed and fresh output.
                    awaiting_output = False

                elif executable:
                    block_number += 1
                    line = start + 1
                    logfile = logs / f"block-{block_number:03d}.log"

                    # Preserve original code fence in rendered docs.
                    rendered.extend(lines[start:end])

                    print(
                        f"\n[{block_number}] "
                        f"{source}:{line} ({language})",
                        flush=True,
                    )

                    try:
                        if language in {"sh", "bash"}:
                            result = execute_bash(
                                token.content,
                                workdir,
                                logfile,
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
                            relative_source = (
                                source.relative_to(repository_root)
                            )
                        except ValueError:
                            relative_source = source

                        github_annotation(
                            relative_source,
                            line,
                            f"{language} block failed: {exc}",
                        )

                        print(
                            f"Failed block {block_number}. "
                            f"Diagnostics: {logfile}",
                            file=sys.stderr,
                        )
                        raise

                    # Insert fresh text output after executable code.
                    # Empty output does not produce a text fence.
                    rendered.append(
                        text_fence(clean(result, repository_root))
                    )

                    awaiting_output = True

                else:
                    # Preserve unlabelled, json, yaml, noexec,
                    # and other non-executable fences unchanged.
                    rendered.extend(lines[start:end])
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
    parser = argparse.ArgumentParser(
        description="Execute Markdown examples and capture outputs"
    )

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
