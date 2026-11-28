"""The Markdown runner, tools/execute_markdown.py, which executes the examples' READMEs.

Apart from the tests of benchx itself: the runner is a development tool that lives in tools/, and
needs markdown-it-py (the `test` extra). These tests skip where either is missing. An installed
benchx has no tools/ (`python -m pytest --pyargs benchx` runs this file from site-packages).
"""

import importlib.util
import json
import os
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


@pytest.mark.parametrize("between", ["## Next\n", "```yaml\na: 1\n```\n"], ids=["heading", "fence"])
def test_a_heading_or_another_fence_ends_the_association(repo, between):
    page = page_of(repo, f"```sh\necho a\n```\n\n{between}\n```text\nKEEP\n```\n")
    assert "KEEP" in page  # no longer a snapshot of the block above: it is an ordinary fence


def test_a_json_snapshot_is_replaced_by_the_output_in_a_json_fence(repo):
    page = page_of(repo, '# T\n\n```sh\necho \'{"new": 1}\'\n```\n\nSome prose.\n\n```json\n{"old": 0}\n```\n\nafter\n')
    assert '```json\n{"new": 1}\n```' in page  # the output, once, in the language of the snapshot
    assert "old" not in page and "```text" not in page
    assert "Some prose." in page and page.rstrip().endswith("after")
    assert page.count("```json") == 1  # the snapshot is gone, not shown next to the output


def test_only_the_first_snapshot_is_replaced(repo):
    page = page_of(repo, "```sh\necho a\n```\n\n```json\nOLD\n```\n\n```text\nKEEP\n```\n\n```json\nKEEP2\n```\n")
    assert "OLD" not in page and "KEEP\n" in page and "KEEP2" in page


def test_a_json_fence_after_a_heading_or_of_another_language_is_not_a_snapshot(repo):
    after_heading = page_of(repo, '```sh\necho a\n```\n\n## Next\n\n```json\n{"keep": 1}\n```\n')
    assert '```json\n{"keep": 1}\n```' in after_heading and "```text\na\n```" in after_heading
    other_language = page_of(repo, "```sh\necho a\n```\n\n```jsonc\n// keep\n```\n")
    assert "```jsonc\n// keep\n```" in other_language and "```text\na\n```" in other_language


def test_noexec_unlabelled_and_other_fences_are_kept_and_not_run(repo):
    text = "```sh noexec\nexit 1\n```\n\n```\n$ pip install x\n```\n\n```json\n{}\n```\n"
    assert page_of(repo, text) == text  # `exit 1` would have failed the run


def test_a_readme_with_no_executable_block_is_copied(repo, capsys):
    text = "# Only prose\n\nNothing to run here.\n"
    assert page_of(repo, text) == text
    assert "No executable blocks" in capsys.readouterr().out


def test_a_dollar_prompt_is_removed_before_the_run_and_kept_on_the_page(repo):
    page = page_of(repo, "```sh\n$ echo one\ntrue\n$ echo two\n```\n")
    assert "```sh\n$ echo one\ntrue\n$ echo two\n```" in page
    assert "```text\none\ntwo\n```" in page


def test_output_containing_backticks_gets_a_longer_fence(repo):
    page = page_of(repo, "```sh\nprintf '%s\\n' '```'\n```\n")
    assert "````text\n```\n````" in page


def test_python_blocks_share_one_namespace(repo):
    page = page_of(repo, "```python\nx = 41\n```\n\n```python\nprint(x + 1)\n```\n")
    assert "```text\n42\n```" in page


def test_stderr_is_part_of_the_output(repo):
    assert "```text\nerr\n```" in page_of(repo, "```sh\necho err >&2\n```\n")


def test_ansi_escapes_and_the_checkout_path_are_left_out_of_the_page_but_not_of_the_log(repo):
    page = page_of(
        repo,
        (
            "```sh\n"
            "printf '\\033[36m$ bx run\\033[0m\\n'\n"
            'echo "config: $(pwd -P)/benchx.conf.json"\n'
            'echo "root: $(cd ../.. && pwd -P)"\n'
            "```\n"
        ),
    )
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
    page = page_of(
        repo,
        ("# T\n\n```sh exit=1\necho before\nfalse\necho never\n```\n\n```text\nOLD\n```\n\n```sh\necho next\n```\n"),
    )
    assert "```sh\necho before\nfalse\necho never\n```" in page  # the block is kept, without its marker
    assert "```text\nbefore\n```" in page  # `false` stopped the script (-e): "never" is not in the output
    assert "OLD" not in page and "```text\nnext\n```" in page  # the snapshot is replaced, the next block ran
    assert "exit=" not in page


def test_the_marker_is_left_out_of_every_fence_of_the_page_and_the_fence_is_kept_as_it_was(repo):
    page = page_of(repo, "~~~sh exit=2\nexit 2\n~~~\n\n```sh noexec exit=1\nexit 1\n```\n")
    assert page == "~~~sh\nexit 2\n~~~\n\n```sh noexec\nexit 1\n```\n"  # a noexec block is not run, so not checked


@pytest.mark.parametrize(
    ("marked", "script", "said"),
    [
        ("exit=1", "true", r"status 0, not the exit=1"),
        ("exit=1", "exit 2", r"status 2, not the exit=1"),
        ("exit=2", "exit 1", r"status 1, not the exit=2"),
        ("exit=0", "exit 1", r"status 1;"),
    ],
    ids=["a-success", "another-failure", "a-lower-one", "zero-is-the-default"],
)
def test_a_block_that_does_not_end_with_its_marked_status_fails_the_run(repo, capsys, marked, script, said):
    source = source_of(repo, f"# T\n\n```sh {marked}\n{script}\n```\n\n```sh\ntouch ran\n```\n")
    output = repo.parent / "out" / "ex.md"
    with pytest.raises(RuntimeError, match=said):
        runner.run(source, output, repo.parent / "logs")
    assert "::error file=examples/ex/README.md,line=3::" in capsys.readouterr().out
    assert not output.exists() and not (repo / "examples" / "ex" / "ran").exists()


@pytest.mark.parametrize(
    "marked",
    ["exit=", "exit=x", "exit=-1", "exit=256", "exit=1 exit=2"],
    ids=["empty", "word", "negative", "too-large", "twice"],
)
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
    args = [
        sys.executable,
        str(TOOL),
        "--source",
        str(source),
        "--output",
        str(output),
        "--logs",
        str(repo.parent / "logs"),
    ]
    done = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "```text\ncli\n```" in output.read_text()
    source.write_text("```sh\nexit 1\n```\n")
    output.unlink()
    failed = subprocess.run(args, cwd=repo, capture_output=True, text=True, check=False)
    assert failed.returncode != 0 and "::error" in failed.stdout and not output.exists()


# The failed results that `bx bench` leaves in the store while it exits 0. A `bx` of the tests
# answers `bx head --json` with the lines of $FAKE_STORE, which a block writes to.


@pytest.fixture
def fake_bx(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bx").write_text(
        "#!/bin/sh\n"
        '[ "$1" = head ] || exit 64\n'
        'if [ -n "$FAKE_BX_ERROR" ]; then echo "$FAKE_BX_ERROR" >&2; exit 2; fi\n'
        'if [ -f "$FAKE_STORE" ]; then cat "$FAKE_STORE"; fi\n'
    )
    (scripts / "bx").chmod(0o755)
    monkeypatch.setenv("PATH", f"{scripts}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STORE", str(tmp_path / "store.jsonl"))


def result(status, case="bench_x", quantity="wall-time", reason=None, kind=None, message=None):
    """One line of `bx head --json`, with the fields that the runner reads."""
    measurement = {"status": status, "quantity": {"name": quantity}}
    measurement.update({k: v for k, v in {"reason": reason, "kind": kind}.items() if v})
    return json.dumps(
        {
            "ingest_key": f"r:{case}:{quantity}:{status}",
            "provenance": {"run_key": "r", "info": {"message": message} if message else {}},
            "coordinates": {"workload": {"name": case, "parameters": {"n": "10"}}},
            "measurement": measurement,
        }
    )


def stores(*lines):
    """A block that appends to the store of the test, as `bx bench` does."""
    return "printf '%s\\n' " + " ".join(f"'{line}'" for line in lines) + ' >> "$FAKE_STORE"'


@pytest.mark.parametrize("status", ["error", "censored", "partial", "skipped"])
def test_a_block_that_exits_0_but_stored_a_failed_result_fails_the_run(repo, fake_bx, status):
    source = source_of(repo, f"```sh\n{stores(result(status, reason='harness.error', message='boom'))}\n```\n")
    output = repo.parent / "out" / "ex.md"
    with pytest.raises(
        RuntimeError,
        match=rf"status 0, but 1 result\(s\) it stored failed:\n  "
        rf"r: bench_x \[n=10\], wall-time: {status} \(harness.error\): boom",
    ):
        runner.run(source, output, repo.parent / "logs")
    assert not output.exists()


def test_results_that_did_not_fail_and_the_load_average_do_not_fail_the_run(repo, fake_bx):
    rows = [
        result("success"),
        result("success", quantity="load-average", kind="condition"),
        result("skipped", quantity="load-average", kind="condition", reason="probe-unavailable"),
    ]
    page_of(repo, f"```sh\n{stores(*rows)}\n```\n")


def test_a_failure_stored_before_a_block_is_not_blamed_on_it_and_a_marked_block_is_not_checked(repo, fake_bx):
    page = page_of(
        repo,
        (
            f"```sh exit=1\n{stores(result('error'))}\nfalse\n```\n\n"
            f"```sh\n{stores(result('success', case='other'))}\n```\n"
        ),
    )
    assert "exit=" not in page


def test_a_new_failure_is_found_though_an_earlier_block_stored_one_too(repo, fake_bx):
    source = source_of(
        repo,
        (
            f"```sh exit=1\n{stores(result('error'))}\nfalse\n```\n\n"
            f"```sh\n{stores(result('error', case='other'))}\n```\n"
        ),
    )
    with pytest.raises(RuntimeError, match=r"1 result\(s\) it stored failed:\n  r: other "):
        runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")


def test_a_store_that_bx_cannot_read_fails_the_block(repo, fake_bx, monkeypatch):
    monkeypatch.setenv("FAKE_BX_ERROR", "requires pyarrow")
    source = source_of(repo, "```sh\ntrue\n```\n")
    with pytest.raises(RuntimeError, match=r"bx head could not read the store \(status 2\): requires pyarrow"):
        runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")


def test_nothing_is_checked_where_bx_is_not_installed(repo, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert runner.failed_results(repo) is None
    assert "```text\nhi\n```" in page_of(repo, "```sh\necho hi\n```\n")


# With the real `bx` and pyperf, which also keeps the runner in step with the stored documents.


@pytest.fixture
def bench_example(repo, monkeypatch):
    pytest.importorskip("pyperf")
    scripts = Path(sys.executable).parent
    if not (scripts / "bx").exists():
        pytest.skip("bx is not installed next to this Python")
    monkeypatch.setenv("PATH", f"{scripts}{os.pathsep}{os.environ['PATH']}")
    example = repo / "examples" / "ex"
    (example / "benchx.conf.json").write_text(
        json.dumps({"schema_version": "benchx/config/0.1.0", "paths": {"store": "out/store.jsonl", "results": "out"}})
    )
    (example / "bench_x.py").write_text(
        "import importlib\n\n\n"
        "def bench_ok():\n    sum(range(10))\n\n\n"
        "def bench_needs_a_library():\n    importlib.import_module('no_such_library_for_the_test')\n"
    )


QUICK = "--processes 1 --values 1 --min-time 0.01"


def test_a_case_that_bx_bench_could_not_measure_fails_the_run(bench_example, repo):
    source = source_of(repo, f"```sh\nbx bench bench_x.py {QUICK} --run-key bad\n```\n")
    with pytest.raises(
        RuntimeError,
        match=r"status 0, but \d+ result\(s\) it stored failed:\n"
        r"  bad: bench_needs_a_library, wall-time: error \(harness.error\): "
        r"ModuleNotFoundError: No module named 'no_such_library_for_the_test'",
    ):
        runner.run(source, repo.parent / "out" / "ex.md", repo.parent / "logs")


def test_cases_that_bx_bench_measured_pass(bench_example, repo):
    page = page_of(repo, f"```sh\nbx bench bench_x.py bench_ok {QUICK} --run-key good\n```\n")
    assert "bench_ok" in page and "success" in page
