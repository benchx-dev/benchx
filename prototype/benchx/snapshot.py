"""What the runner records about a run (benchmark-environments.md §3).

Every reader returns only what it could read; an unreadable value is absent,
never a placeholder (§3.1). Nothing here changes the machine.
"""

import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import tempfile
from pathlib import Path

from .identity import normalize_remote

# §3.1: never the whole environment; only variables that shape execution: the
# thread and device variables, and the dynamic loader's search variables. No
# build or environment management tool is named; a project extends the list
# with $BENCHX_ENV_ALLOWLIST (comma-separated names), and an adapter adds the
# variables its ecosystem cares about (its ENV_ALLOWLIST).
ENV_ALLOWLIST = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "CUDA_VISIBLE_DEVICES",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "LD_PRELOAD",
)


def env_allowlist(env: dict, adapter_names=()) -> tuple:
    """Core default, then the project's names from `env`, then the adapter's."""
    extra = [n.strip() for n in env.get("BENCHX_ENV_ALLOWLIST", "").split(",") if n.strip()]
    names = list(ENV_ALLOWLIST)
    names += [n for n in (*extra, *adapter_names) if n not in names]
    return tuple(names)


def _run(args, cwd=None):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _cpu_model():
    if platform.system() == "Darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    text = _read("/proc/cpuinfo") or ""
    match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def environment() -> dict:
    """Environment identity under machine/v1: host name, CPU model, cores; and
    the architecture, the first fact of the system it runs (schema §4.2)."""
    identity = {"runner": socket.gethostname()}
    if model := _cpu_model():
        identity["cpu"] = model
    if cores := os.cpu_count():
        identity["cores"] = cores
    return {"schema": "machine/v1", "identity": identity,
            "system": {"architecture": platform.machine()}}


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
    env = {k: child_env[k] for k in (env_names or env_allowlist(child_env)) if k in child_env}
    if env:
        facts["process"] = {"environment": env}
    return facts


def load_average() -> float | None:
    """The 1-minute load average, or None where the OS has none (Windows)."""
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def merge(base: dict, facts: dict) -> dict:
    """`facts` over `base`, object by object; any other value replaces."""
    out = dict(base)
    for key, value in facts.items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


# Bytecode caches are written by running the suite, so they must not change the tree id.
_PYCACHE_EXCLUDE = ":(exclude,glob)**/__pycache__/**"


_IGNORED: tuple[Path, ...] = ()


def ignore(*paths) -> None:
    """Paths benchx itself writes (the store, the result files), left out of every
    tree identity from now on, so a run's own output never marks the tree dirty
    or changes its tree id. Replaces the previous set; no arguments clears it."""
    global _IGNORED
    _IGNORED = tuple(Path(p).resolve() for p in paths)


def _ignored_in(root: Path) -> list[str]:
    """The ignored paths inside `root`, relative to it."""
    root = Path(root).resolve()
    return [p.relative_to(root).as_posix() for p in _IGNORED if p != root and p.is_relative_to(root)]


def _git_ignores(top, rel) -> bool:
    """Whether the repository's own ignore rules already leave `rel` out of `git add -A`.
    Naming such a path in an exclude pathspec makes `git add` fail, and the tree id
    with it, so a results directory the project git-ignores must not be excluded twice."""
    return subprocess.run(["git", "check-ignore", "-q", "--", rel], cwd=top,
                          capture_output=True).returncode == 0


def git_identity(path) -> dict | None:
    """Revision, dirty state, and working-tree id of a checkout (schema §4.1)."""
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    head = top and _run(["git", "rev-parse", "HEAD"], cwd=top)
    if not head:
        return None
    excludes = [_PYCACHE_EXCLUDE, *(f":(top,exclude){rel}" for rel in _ignored_in(top)
                                    if not _git_ignores(top, rel))]
    status = _run(["git", "status", "--porcelain", "--untracked-files=all", "--", ".", *excludes], cwd=top)
    # A scratch index that does not exist yet, so the repository's own index
    # is untouched (schema §4.1).
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        env = {**os.environ, "GIT_INDEX_FILE": index}
        try:
            subprocess.run(["git", "add", "-A", "--", ".", *excludes], cwd=top, env=env, check=True, capture_output=True)
            tree = subprocess.run(["git", "write-tree"], cwd=top, env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            tree = None
    identity = {"path": top, "revision": head}
    if tree is not None and status is not None:
        identity.update(dirty="dirty" if status else "clean", tree=tree)
    else:
        identity["dirty"] = "unknown"
    return identity


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
        return None
    tree = blob_id(suite)
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=suite.parent)
    head = top and _run(["git", "rev-parse", "HEAD"], cwd=top)
    if head:
        committed = _run(["git", "rev-parse", f"HEAD:{suite.relative_to(Path(top).resolve()).as_posix()}"], cwd=top)
        return {"uri": source_uri(top), "type": "git", "revision": head, "tree": tree,
                "dirty": "clean" if committed == tree else "dirty"}
    return {"uri": suite.parent.as_uri(), "type": "directory", "tree": tree, "dirty": "dirty"}


_SKIPPED_DIRS = {".git", ".benchx", "__pycache__"}


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
    digest = hashlib.sha1()
    ignored = tuple(_IGNORED)
    for file in sorted(p for p in root.rglob("*") if p.is_file()
                       and not _SKIPPED_DIRS.intersection(p.relative_to(root).parts)
                       and not any(p.resolve().is_relative_to(i) for i in ignored)):
        try:
            content = file.read_bytes()
        except OSError:
            return None
        name = file.relative_to(root).as_posix().encode()
        digest.update(b"%d:%s:%d:" % (len(name), name, len(content)) + content)
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
    for key, name in (("CMAKE_BUILD_TYPE", "build_type"), ("CMAKE_CXX_FLAGS", "cxx_flags")):
        if cache.get(key, (None, ""))[1]:
            config[name] = cache[key][1]
    compiler_file = next(Path(build_dir).glob("CMakeFiles/*/CMakeCXXCompiler.cmake"), None)
    text = _read(compiler_file) if compiler_file else None
    if text:
        found = dict(re.findall(r'set\(CMAKE_CXX_COMPILER_(ID|VERSION) "([^"]*)"\)', text))
        if found.get("ID"):
            config["cxx_compiler"] = f"{found['ID']} {found.get('VERSION', '')}".strip()
    project = cache.get("CMAKE_PROJECT_NAME", (None, ""))[1]
    if project:
        prefix = re.sub(r"[^A-Z0-9]", "_", project.upper()) + "_"
        options = {k: v for k, (kind, v) in sorted(cache.items())
                   if k.startswith(prefix) and kind in ("BOOL", "STRING") and "/" not in v}
        if options:
            config["options"] = options
    return config


def setup_facts(suite_root, env) -> tuple[dict | None, str | None]:
    """Declared facts from the setup file (harness-adapter.md §5.1).

    Returns (facts, warning). An absent file is not an error; an unreadable
    one contributes nothing and yields the warning collector-failed.
    """
    path = Path(env.get("BENCHX_SETUP_FILE") or Path(suite_root) / ".benchx" / "setup.json")
    if not path.exists():
        return None, None
    try:
        facts = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, "collector-failed"
    return (facts, None) if isinstance(facts, dict) else (None, "collector-failed")
