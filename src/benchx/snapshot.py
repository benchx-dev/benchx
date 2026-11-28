"""What the runner records about a run (benchmark-environments.md §3).

Every reader returns only what it could read; an unreadable value is absent,
never a placeholder (§3.1). Nothing here changes the machine.
"""

import hashlib
import json
import logging
import os
import platform
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from . import log
from .identity import normalize_remote

logger = logging.getLogger(__name__)

# §3.1: never the whole environment; only variables that shape execution: the
# thread and device variables, and the dynamic loader's search variables. No
# build or environment management tool is named; a project extends the list
# with $BENCHX_ENV_ALLOWLIST (comma-separated names), and an adapter adds the
# variables its ecosystem cares about (its ENV_ALLOWLIST).
ENV_ALLOWLIST = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "CUDA_VISIBLE_DEVICES",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "LD_PRELOAD",
)


# The quality warning a result carries for each dirty flag that is `dirty` (schema §4.1).
DIRTY_WARNINGS = (("subject_dirty", "dirty-subject-tree"), ("benchmark_dirty", "dirty-benchmark-tree"))


def env_allowlist(env: dict, adapter_names=()) -> tuple:
    """Core default, then the project's names from `env`, then the adapter's."""
    extra = [n.strip() for n in env.get("BENCHX_ENV_ALLOWLIST", "").split(",") if n.strip()]
    names = list(ENV_ALLOWLIST)
    names += [n for n in (*extra, *adapter_names) if n not in names]
    return tuple(names)


def _run(args, cwd=None, raw=False):
    """The output of a command, stripped (`raw`: only its last newline, for output whose first column counts)."""
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    except OSError as e:
        logger.debug("%s failed in %s: %s", log.command(args), cwd or Path.cwd(), e)
        return None
    except subprocess.CalledProcessError as e:
        logger.debug(
            "%s failed in %s: exit %d: %s",
            log.command(args),
            cwd or Path.cwd(),
            e.returncode,
            " | ".join((e.stderr or "").strip().splitlines()[:3]),
        )
        return None
    return out.stdout.rstrip("\n") if raw else out.stdout.strip()


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        if log.once(("unreadable", str(path))):  # read for every order: say it once per command
            logger.debug("cannot read %s: %s", path, e.strerror or e)
        return None


def _cpu_model():
    if platform.system() == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    text = _read("/proc/cpuinfo") or ""
    match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
    if not match and log.once("no-cpu-model"):
        logger.debug(
            "no CPU model found (no `model name` in /proc/cpuinfo, as on ARM Linux): left out of the "
            "environment identity"
        )
    return match.group(1).strip() if match else None


def environment() -> dict:
    """Environment identity under machine/v1: host name, CPU model, cores; and
    the architecture, the first fact of the system it runs (schema §4.2)."""
    identity = {"runner": socket.gethostname()}
    if model := _cpu_model():
        identity["cpu"] = model
    if cores := os.cpu_count():
        identity["cores"] = cores
    return {"schema_version": "machine/v1", "identity": identity, "system": {"architecture": platform.machine()}}


def system_facts(child_env: dict, env_names=None) -> dict:
    """Facts about the system the measured process runs on, as placed under
    `environment.system` (schema §4.2, Appendix B). The default identity
    policy ignores os, libc and cpu, and matches the process environment."""
    facts = {"os": {"family": platform.system(), "kernel": platform.release()}}
    libc, version = platform.libc_ver()
    if libc:
        facts["libc"] = {"name": libc, "version": version}
    if governor := _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"):
        facts["cpu"] = {"governor": governor}
    left_out = [name for name, found in (("libc", libc), ("cpu governor", "cpu" in facts)) if not found]
    if left_out and log.once("facts-left-out"):
        logger.debug("system facts left out, because they could not be read: %s", ", ".join(left_out))
    env = {k: child_env[k] for k in (env_names or env_allowlist(child_env)) if k in child_env}
    if env:
        facts["process"] = {"environment": env}
    return facts


def load_average() -> float | None:
    """The 1-minute load average, or None where the OS has none (Windows)."""
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        if log.once("no-load-average"):  # asked for every case: said once per command, the case line shows `load n/a`
            logger.debug("the load average is unavailable on this system")
        return None


def merge(base: dict, facts: dict) -> dict:
    """`facts` over `base`, object by object; any other value replaces."""
    out = dict(base)
    for key, value in facts.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


# Bytecode caches are written by running the suite, so they must not change the tree id.
_PYCACHE_EXCLUDE = ":(exclude,glob)**/__pycache__/**"

# The same folders, and `out/`, where a script gives each `bx bench` call its own results directory,
# left out of the list of untracked paths the log names (`out/` would head it in every run). These are
# ignore patterns (`ls-files --exclude`), not exclude pathspecs: with `--directory`, git up to 2.27
# still names `pkg/` when all it holds is `pkg/sub/__pycache__/`, as the pathspec is not applied
# when deciding whether a collapsed directory is empty; ignore patterns are applied as git walks.
_UNTRACKED_IGNORES = ("--exclude=__pycache__/", "--exclude=out/")


_IGNORED: tuple[Path, ...] = ()


def ignore(*paths) -> None:
    """Paths benchx itself writes (the store, the result files), left out of every
    tree identity from now on, so a run's own output never marks the tree dirty
    or changes its tree id. Replaces the previous set; no arguments clears it."""
    global _IGNORED
    _IGNORED = tuple(Path(p).resolve() for p in paths)
    logger.debug("left out of every tree identity from now on: %s", ", ".join(map(str, _IGNORED)) or "nothing")


def _ignored_in(root: Path) -> list[str]:
    """The ignored paths inside `root`, relative to it."""
    root = Path(root).resolve()
    return [p.relative_to(root).as_posix() for p in _IGNORED if p != root and p.is_relative_to(root)]


def _git_ignores(top, rel) -> bool:
    """Whether the repository's own ignore rules already leave `rel` out of `git add -A`.
    Naming such a path in an exclude pathspec makes `git add` fail, and the tree id
    with it, so a results directory the project git-ignores must not be excluded twice."""
    return subprocess.run(["git", "check-ignore", "-q", "--", rel], cwd=top, capture_output=True).returncode == 0


def git_identity(path) -> dict | None:
    """Revision, dirty state, and working-tree id of a checkout (schema §4.1), for its tracked files: it is
    `dirty` when one of them differs from HEAD (an edit, a deletion, a mode change, a submodule at another
    commit), and its tree id is the tree of its tracked files as they are now, what is staged included (`git add`,
    `git mv`, `git rm --cached`), so a clean checkout has HEAD's own tree id. Untracked files are not part of the
    identity: they are listed in `untracked`, for the log."""
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    head = top and _run(["git", "rev-parse", "HEAD"], cwd=top)
    if not head:
        return None
    own = [(rel, _git_ignores(top, rel)) for rel in _ignored_in(top)]
    excludes = [_PYCACHE_EXCLUDE, *(f":(top,exclude){rel}" for rel, git_does in own if not git_does)]
    logger.debug(
        "identity of %s: its tracked files only; leaves out __pycache__ and benchx's own paths %s (git's own "
        "ignore rules already cover %s)",
        top,
        [r for r, g in own if not g] or "(none inside it)",
        [r for r, g in own if g] or "none",
    )
    status = _run(["git", "status", "--porcelain", "--untracked-files=no", "--", ".", *excludes], cwd=top, raw=True)
    # What is not tracked, for the log: collapse untracked directories without re-diffing tracked files or submodules.
    listed = _run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            *_UNTRACKED_IGNORES,
            "--directory",
            "--no-empty-directory",
            "--",
            ".",
            *excludes,
        ],
        cwd=top,
        raw=True,
    )
    untracked = (listed or "").splitlines()
    # A scratch copy of the repository's own index, so the repository's index is untouched (schema §4.1): what is
    # staged (`git add`, `git mv`, `git rm --cached`) stays as staged, then the tracked files as they are now.
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        env = {**os.environ, "GIT_INDEX_FILE": index}
        try:
            real_index = _run(["git", "rev-parse", "--git-path", "index"], cwd=top)
            if real_index and (Path(top) / real_index).exists():
                Path(index).write_bytes((Path(top) / real_index).read_bytes())
            else:
                subprocess.run(["git", "read-tree", "HEAD"], cwd=top, env=env, check=True, capture_output=True)
            subprocess.run(
                ["git", "add", "-u", "--", ".", *excludes], cwd=top, env=env, check=True, capture_output=True
            )
            tree = subprocess.run(
                ["git", "write-tree"], cwd=top, env=env, check=True, capture_output=True, text=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as e:
            why_tree = " | ".join(
                (getattr(e, "stderr", None) or b"").decode(errors="replace").strip().splitlines()[:2]
            ) or str(e)
            logger.debug("git add or write-tree failed in %s: %s", top, why_tree)
            tree = None
    identity = {"path": top, "revision": head}
    if tree is not None and status is not None:
        identity.update(dirty="dirty" if status else "clean", tree=tree)
        if status:
            identity["changes"] = status.splitlines()  # why the tree is dirty, for the log
        if untracked:
            identity["untracked"] = untracked
    else:
        identity["dirty"] = "unknown"
        identity["why_unknown"] = "git status failed" if status is None else "git add or write-tree failed"
    return identity


def note_identity_fallback(identity: dict) -> None:
    """A working tree identified by a content hash because git could not: INFO when the directory has no
    `.git`, WARNING when it has one and git failed. Its revision is `directory-<hash>` and its state always
    dirty, which the record says. Once per directory and command."""
    if not identity["revision"].startswith("directory-") or not log.once(("fallback", identity["path"])):
        return
    has_git = (Path(identity["path"]) / ".git").exists()
    logger.log(
        logging.WARNING if has_git else logging.INFO,
        "%s is identified by a hash of its files (method directory-hash), as %s: its revision is %s and its "
        "state is always dirty",
        identity["path"],
        "git could not read it" if has_git else "it is not under git",
        identity["revision"][:28],
    )


def why_no_checkout(build_dir, source_dir=None) -> str:
    """Why the checkout a build was made from cannot be found, for the refusal that says so."""
    cache = Path(build_dir) / "CMakeCache.txt"
    if not cache.exists():
        return f"there is no CMakeCache.txt in {build_dir}"
    if not source_dir:
        return "CMakeCache.txt has no CMAKE_HOME_DIRECTORY"
    if not Path(source_dir).is_dir():
        return f"the recorded source directory {source_dir} is gone"
    return f"git could not read {source_dir}"


def source_uri(source_dir) -> str:
    """One canonical URI for every worktree of a repository, so both sides of a
    comparison report the same source: the origin remote, else the shared git
    directory's parent, else the directory itself."""
    origin = _run(["git", "remote", "get-url", "origin"], cwd=source_dir)
    if origin:
        return normalize_remote(origin, base=source_dir)
    common = _run(["git", "rev-parse", "--git-common-dir"], cwd=source_dir)
    return Path(source_dir, common).resolve().parent.as_uri() if common else Path(source_dir).resolve().as_uri()


def blob_id(path) -> str | None:
    """The git blob id of a file: what `git hash-object FILE` prints."""
    try:
        content = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def benchmark_identity(suite_file, subject_source) -> dict | None:
    """Where the suite's own code lives, when that is not the subject's checkout:
    None when the file is inside `subject_source`.

    The file is the unit, not its directory (runner-schema.md §3.3). Its tree id
    is its blob id. In a git checkout the source is that repository, with its
    HEAD as the revision, and the file is `clean` when it equals HEAD's version
    and `dirty` otherwise, including when HEAD has none. Outside git the source
    is the file's directory as a `file://` URI, with no revision, and the file is
    `dirty`: nothing says it was ever committed.
    """
    suite = Path(suite_file).resolve()
    if suite.is_relative_to(Path(subject_source).resolve()):
        logger.debug("the suite %s is in the subject's checkout: its identity is the subject's", suite)
        return None
    tree = blob_id(suite)
    if tree is None:
        logger.warning("the suite file %s cannot be read, so its identity has no blob id", suite)
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=suite.parent)
    head = top and _run(["git", "rev-parse", "HEAD"], cwd=top)
    if head:
        committed = _run(["git", "rev-parse", f"HEAD:{suite.relative_to(Path(top).resolve()).as_posix()}"], cwd=top)
        return {
            "uri": source_uri(top),
            "type": "git",
            "revision": head,
            "tree": tree,
            "dirty": "clean" if committed == tree else "dirty",
        }
    logger.debug(
        "the suite %s is under no version control: a file:// source, always dirty; its series does not "
        "follow the suite to another machine",
        suite,
    )
    return {"uri": suite.parent.as_uri(), "type": "directory", "tree": tree, "dirty": "dirty"}


_SKIPPED_DIRS = {".git", ".benchx", "__pycache__", "out"}


def directory_identity(path) -> dict | None:
    """Identity of a checkout that is not under git (schema §4.1, non-git sources).

    The tree id is a SHA-1 over the sorted relative paths and file contents,
    shaped like a git tree id. There is no commit, so the revision key is
    derived from the tree, and the state is `dirty`: nothing says this content
    was ever committed, so a comparator must not treat it as a clean revision.
    """
    root = Path(path)
    if not root.is_dir():
        return None
    started, files, size = time.monotonic(), 0, 0
    digest = hashlib.sha1()
    ignored = tuple(_IGNORED)
    for file in sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and not _SKIPPED_DIRS.intersection(p.relative_to(root).parts)
        and not any(p.resolve().is_relative_to(i) for i in ignored)
    ):
        try:
            content = file.read_bytes()
        except OSError as e:
            logger.warning("cannot read %s, so the tree %s cannot be identified: %s", file, root, e.strerror or e)
            return None
        name = file.relative_to(root).as_posix().encode()
        digest.update(b"%d:%s:%d:" % (len(name), name, len(content)) + content)
        files, size = files + 1, size + len(content)
    logger.debug(
        "hashed %d file(s), %d byte(s) of %s in %.2f s; left out: %s (always), and benchx's own paths",
        files,
        size,
        root,
        time.monotonic() - started,
        ", ".join(sorted(_SKIPPED_DIRS)),
    )
    tree = digest.hexdigest()
    return {"path": str(root.resolve()), "revision": f"directory-{tree}", "dirty": "dirty", "tree": tree}


def _cmake_cache(build_dir) -> dict:
    entries = {}
    text = _read(Path(build_dir) / "CMakeCache.txt") or ""
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_.+-]+):([A-Z]+)=(.*)$", line)
        if match:
            entries[match.group(1)] = (match.group(2), match.group(3))
    return entries


def cmake_source_dir(build_dir) -> str | None:
    entry = _cmake_cache(build_dir).get("CMAKE_HOME_DIRECTORY")
    if not entry and log.once(("no-source-dir", str(build_dir))):
        logger.debug("%s has no CMakeCache.txt, or no CMAKE_HOME_DIRECTORY in it: no source directory", build_dir)
    return entry[1] if entry else None


def cmake_configuration(build_dir) -> dict:
    """Built-in plugin: the build configuration behind a CMake build directory.

    Build type, compiler, flags, and the project's own options, meaning cache
    entries prefixed with the upper-cased project name (ARROW_* for Arrow).
    Never paths, so two directories configured the same way report the same
    configuration.
    """
    cache = _cmake_cache(build_dir)
    config = {}
    if not cache:
        _once_warn(
            ("no-cache", str(build_dir)),
            "no CMakeCache.txt in %s: no build configuration is recorded, so "
            "`build-config-unverified` has nothing behind it",
            build_dir,
        )
    for key, name in (("CMAKE_BUILD_TYPE", "build_type"), ("CMAKE_CXX_FLAGS", "cxx_flags")):
        if cache.get(key, (None, ""))[1]:
            config[name] = cache[key][1]
    compiler_file = next(Path(build_dir).glob("CMakeFiles/*/CMakeCXXCompiler.cmake"), None)
    if cache and compiler_file is None:
        _once_warn(
            ("no-compiler-file", str(build_dir)),
            "no CMakeCXXCompiler.cmake under %s/CMakeFiles: the compiler is not recorded",
            build_dir,
        )
    text = _read(compiler_file) if compiler_file else None
    if text:
        found = dict(re.findall(r'set\(CMAKE_CXX_COMPILER_(ID|VERSION) "([^"]*)"\)', text))
        if found.get("ID"):
            config["cxx_compiler"] = f"{found['ID']} {found.get('VERSION', '')}".strip()
    project = cache.get("CMAKE_PROJECT_NAME", (None, ""))[1]
    if project:
        prefix = re.sub(r"[^A-Z0-9]", "_", project.upper()) + "_"
        options = {
            k: v
            for k, (kind, v) in sorted(cache.items())
            if k.startswith(prefix) and kind in ("BOOL", "STRING") and "/" not in v
        }
        if options:
            config["options"] = options
    if log.once(("cmake", str(build_dir))):
        logger.debug(
            "CMake configuration of %s: recorded %s; from %d cache entries, %d option(s) with the prefix of "
            "the project %r kept",
            build_dir,
            ", ".join(sorted(config)) or "nothing",
            len(cache),
            len(config.get("options", {})),
            cache.get("CMAKE_PROJECT_NAME", (None, ""))[1],
        )
    return config


def _once_warn(key, message, *args) -> None:
    """A WARNING the first time, DEBUG after: what is the same for every order of a command."""
    logger.log(logging.WARNING if log.once(key) else logging.DEBUG, message, *args)


def setup_facts(suite_root, env) -> tuple[dict | None, str | None]:
    """Declared facts from the setup file (harness-adapter.md §5.1).

    Returns (facts, warning). An absent file is not an error; an unreadable
    one contributes nothing and yields the warning collector-failed.
    """
    named = env.get("BENCHX_SETUP_FILE")
    path = Path(named or Path(suite_root) / ".benchx" / "setup.json")
    # BENCHX_SETUP_FILE is one of benchx's own control variables, which the log gives with their values
    # (cli.CONTROL_ENV): the path is named wherever the file is.
    shown = str(path)
    if not path.exists():
        if named:
            _once_warn("setup-missing", "BENCHX_SETUP_FILE names a file that does not exist (%s): no setup facts", path)
        return None, None
    try:
        facts = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        _once_warn(
            ("setup-unreadable", shown),
            "the setup file (%s) could not be read, so it gives the results the warning collector-failed: %s",
            shown,
            e,
        )
        return None, "collector-failed"
    if not isinstance(facts, dict):
        _once_warn(
            ("setup-not-object", shown),
            "the setup file (%s) holds JSON that is not an object, so it gives the "
            "results the warning collector-failed",
            shown,
        )
        return None, "collector-failed"
    return facts, None
