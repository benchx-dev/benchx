"""The Markdown runner, tools/execute_markdown.py, which executes the examples' READMEs.

Apart from the tests of benchx itself: the runner is a development tool that lives in tools/, and
needs markdown-it-py (the `test` extra). These tests skip where either is missing. An installed
benchx has no tools/ (`python -m pytest --pyargs benchx` runs this file from site-packages).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("markdown_it")

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "execute_markdown.py"
if not TOOL.is_file():
    pytest.skip("tools/execute_markdown.py is not part of an installed benchx", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("execute_markdown", TOOL)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository root with examples/ex/, which is also where the runner is started from.

    The page and the logs go next to it, not into it, as they do in CI.
    """
    root = tmp_path / "repo"
    (root / "examples" / "ex").mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


def source_of(repo, text):
    source = repo / "examples" / "ex" / "README.md"
    source.write_text(text)
    return source


def page_of(repo, text):
    """Execute `text` as the README of the example, and return the page."""
    source = source_of(repo, text)
    output = repo.parent / "out" / "ex.md"
    runner.run(source, output, repo.parent / "logs")
    return output.read_text()


def test_fresh_output_replaces_the_committed_snapshot(repo):
    page = page_of(repo, "# T\n\n```sh\necho hello\n```\n\n```text\nOLD\n```\n\nafter\n")
    assert "```sh\necho hello\n```" in page  # the block is kept
    assert "```text\nhello\n```" in page
    assert "OLD" not in page and page.rstrip().endswith("after")


def test_prose_may_come_between_a_block_and_its_snapshot(repo):
    page = page_of(repo, "```sh\necho a\n```\n\nSome prose.\n\n```text\nOLD\n```\n")
    assert "```text\na\n```" in page and "Some prose." in page and "OLD" not in page


@pytest.mark.parametrize("between", ["## Next\n", "```json\n{}\n```\n"], ids=["heading", "fence"])
def test_a_heading_or_another_fence_ends_the_association(repo, between):
    page = page_of(repo, f"```sh\necho a\n```\n\n{between}\n```text\nKEEP\n```\n")
    assert "KEEP" in page  # no longer a snapshot of the block above: it is an ordinary fence


def test_noexec_unlabelled_and_other_fences_are_kept_and_not_run(repo):
    text = "```sh noexec\nexit 1\n```\n\n```\n$ pip install x\n```\n\n```json\n{}\n```\n"
    assert page_of(repo, text) == text  # `exit 1` would have failed the run


def test_a_readme_with_no_executable_block_is_copied(repo, capsys):
    text = "# Only prose\n\nNothing to run here.\n"
    assert page_of(repo, text) == text
    assert "No executable blocks" in capsys.readouterr().out


def test_output_containing_backticks_gets_a_longer_fence(repo):
    page = page_of(repo, "```sh\nprintf '%s\\n' '```'\n```\n")
    assert "````text\n```\n````" in page


def test_python_blocks_share_one_namespace(repo):
    page = page_of(repo, "```python\nx = 41\n```\n\n```python\nprint(x + 1)\n```\n")
    assert "```text\n42\n```" in page


def test_stderr_is_part_of_the_output(repo):
    assert "```text\nerr\n```" in page_of(repo, "```sh\necho err >&2\n```\n")


def test_ansi_escapes_and_the_checkout_path_are_left_out_of_the_page_but_not_of_the_log(repo):
    page = page_of(repo, (
        "```sh\n"
        "printf '\\033[36m$ bx run\\033[0m\\n'\n"
        'echo "config: $(pwd -P)/benchx.conf.json"\n'
        'echo "root: $(cd ../.. && pwd -P)"\n'
        "```\n"))
    assert "```text\n$ bx run\nconfig: examples/ex/benchx.conf.json\nroot: .\n```" in page
    assert "\x1b" not in page and str(repo) not in page
    log = (repo.parent / "logs" / "block-001.log").read_text()
    assert "\x1b[36m" in log and str(repo) in log


def test_a_failing_block_raises_with_an_annotation_and_writes_no_page(repo, capsys):
    source = source_of(repo, "# T\n\n```sh\nexit 3\n```\n\n```sh\ntouch ran\n```\n")
    output = repo.parent / "out" / "ex.md"
    with pytest.raises(RuntimeError, match="status 3"):
        runner.run(source, output, repo.parent / "logs")
    assert "::error file=examples/ex/README.md,line=3::" in capsys.readouterr().out
    assert not output.exists()
    assert not (repo / "examples" / "ex" / "ran").exists()  # the blocks after it did not run


def test_a_block_marked_exit_is_expected_to_fail_and_the_run_goes_on(repo):
    page = page_of(repo, (
        "# T\n\n```sh exit=1\necho before\nfalse\necho never\n```\n\n```text\nOLD\n```\n\n"
        "```sh\necho next\n```\n"))
    assert "```sh\necho before\nfalse\necho never\n```" in page  # the block is kept, without its marker
    assert "```text\nbefore\n```" in page  # `false` stopped the script (-e): "never" is not in the output
    assert "OLD" not in page and "```text\nnext\n```" in page  # the snapshot is replaced, the next block ran
    assert "exit=" not in page


def test_the_marker_is_left_out_of_every_fence_of_the_page_and_the_fence_is_kept_as_it_was(repo):
    page = page_of(repo, "~~~sh exit=2\nexit 2\n~~~\n\n```sh noexec exit=1\nexit 1\n```\n")
    assert page == "~~~sh\nexit 2\n~~~\n\n```sh noexec\nexit 1\n```\n"  # a noexec block is not run, so not checked


@pytest.mark.parametrize(("marked", "script", "said"), [
    ("exit=1", "true", r"status 0, not the exit=1"),
    ("exit=1", "exit 2", r"status 2, not the exit=1"),
    ("exit=2", "exit 1", r"status 1, not the exit=2"),
    ("exit=0", "exit 1", r"status 1;"),
], ids=["a-success", "another-failure", "a-lower-one", "zero-is-the-default"])
def test_a_block_that_does_not_end_with_its_marked_status_fails_the_run(repo, capsys, marked, script, said):
    source = source_of(repo, f"# T\n\n```sh {marked}\n{script}\n```\n\n```sh\ntouch ran\n```\n")
    output = repo.parent / "out" / "ex.md"
    with pytest.raises(RuntimeError, match=said):
        runner.run(source, output, repo.parent / "logs")
    assert "::error file=examples/ex/README.md,line=3::" in capsys.readouterr().out
    assert not output.exists() and not (repo / "examples" / "ex" / "ran").exists()


@pytest.mark.parametrize("marked", ["exit=", "exit=x", "exit=-1", "exit=256", "exit=1 exit=2"],
                         ids=["empty", "word", "negative", "too-large", "twice"])
def test_a_malformed_marker_fails_the_run_before_the_block_runs(repo, capsys, marked):
    source = source_of(repo, f"```sh {marked}\ntouch ran\n```\n")
    with pytest.raises(ValueError, match="exit="):
        runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")
    assert "::error file=examples/ex/README.md,line=1::" in capsys.readouterr().out
    assert not (repo / "examples" / "ex" / "ran").exists()


def test_a_python_block_cannot_be_marked_exit(repo):
    source = source_of(repo, "```python exit=1\nopen('ran', 'w')\n```\n")
    with pytest.raises(ValueError, match="only sh and bash"):
        runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")
    assert not (repo / "examples" / "ex" / "ran").exists()


def test_a_failing_python_block_raises_and_its_traceback_is_in_the_log(repo):
    source = source_of(repo, "```python\nraise ValueError('boom')\n```\n")
    output = repo.parent / "out" / "ex.md"
    with pytest.raises(ValueError, match="boom"):
        runner.run(source, output, repo.parent / "logs")
    assert "ValueError: boom" in (repo.parent / "logs" / "block-001.log").read_text()
    assert not output.exists()


def test_the_source_is_not_modified_and_cannot_be_the_output(repo):
    text = "```sh\necho hi\n```\n\n```text\nOLD\n```\n"
    source = source_of(repo, text)
    runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")
    assert source.read_text() == text
    with pytest.raises(ValueError, match="differ"):
        runner.run(source, source, repo.parent / "logs")
    assert source.read_text() == text


def test_the_command_line(repo):
    source = source_of(repo, "```sh\necho cli\n```\n")
    output = repo.parent / "out" / "ex.md"
    args = [sys.executable, str(TOOL), "--source", str(source), "--output", str(output),
            "--logs", str(repo.parent / "logs")]
    done = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "```text\ncli\n```" in output.read_text()
    source.write_text("```sh\nexit 1\n```\n")
    output.unlink()
    failed = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
    assert failed.returncode != 0 and "::error" in failed.stdout and not output.exists()
