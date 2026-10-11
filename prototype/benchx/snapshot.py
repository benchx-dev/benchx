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
    """Environment identity under machine/v1: host name, CPU model, cores."""
    identity = {"runner": socket.gethostname()}
    if model := _cpu_model():
        identity["cpu"] = model
    if cores := os.cpu_count():
        identity["cores"] = cores
    return {"schema": "machine/v1", "identity": identity,
            "metadata": {"architecture": platform.machine()}}


def observed_context(child_env: dict, env_names=None) -> dict:
    """Conditions allowed to vary within a series (§3.4)."""
    facts = {"os": platform.system(), "kernel": platform.release()}
    libc, version = platform.libc_ver()
    if libc:
        facts["libc"] = f"{libc}-{version}"
    if governor := _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"):
        facts["governor"] = governor
    try:
        facts["load_avg_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    env = {k: child_env[k] for k in (env_names or env_allowlist(child_env)) if k in child_env}
    if env:
        facts["env"] = env
    return facts


def git_identity(path) -> dict | None:
    """Revision, dirty state, and working-tree id of a checkout (schema §4.1)."""
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd=path)
    head = top and _run(["git", "rev-parse", "HEAD"], cwd=top)
    if not head:
        return None
    status = _run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=top)
    # A scratch index that does not exist yet, so the repository's own index
    # is untouched (schema §4.1).
    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "index")
        env = {**os.environ, "GIT_INDEX_FILE": index}
        try:
            subprocess.run(["git", "add", "-A"], cwd=top, env=env, check=True, capture_output=True)
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
    for file in sorted(p for p in root.rglob("*") if p.is_file()
                       and not _SKIPPED_DIRS.intersection(p.relative_to(root).parts)):
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
