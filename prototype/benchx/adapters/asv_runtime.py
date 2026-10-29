"""ASV's benchmark language, read and run without asv (harness-adapter.md §6.7).

Standalone: the standard library only, so the adapter copies this file beside
each case's driver and the benchmark's interpreter imports it from there.

This is the part of asv that gives a benchmark file its meaning: which
functions and methods are benchmarks, how attributes resolve (the function,
then the class instance, then the module), `params` and `param_names`, the
`setup`/`teardown`/`setup_cache` hooks, skipping by `NotImplementedError` or
`skip_params`, and the `version` hash of the benchmark's code. It follows
asv_runner 0.3 (discovery.py, benchmarks/_base.py, benchmarks/time.py) so that
names, parameter strings and versions agree with what asv itself reports.
Timing is not here: the driver hands `time_func` to pyperf.
"""

import gc
import hashlib
import importlib
import importlib.machinery
import inspect
import itertools
import json
import os
import pkgutil
import re
import sys
import textwrap
import time
import traceback
from collections import Counter

# asv's benchmark types and their `type` labels; the first whose regex matches wins.
TYPES = (
    ("memory", re.compile("^(Mem[A-Z_].+)|(mem_.+)$")),
    ("peakmemory", re.compile("^(PeakMem[A-Z_].+)|(peakmem_.+)$")),
    ("time", re.compile("^(Time[A-Z_].+)|(time_.+)$")),
    ("timeraw", re.compile("^(Timeraw[A-Z_].+)|(timeraw_.+)$")),
    ("track", re.compile("^(Track[A-Z_].+)|(track_.+)$")),
)
# Declared attributes that bear on how a case is timed.
DECLARED = ("number", "repeat", "rounds", "processes", "sample_time", "warmup_time", "min_run_count", "timeout",
            "setup_scope")


class Skipped(Exception):
    """The case does not apply: `setup` raised NotImplementedError, or the combination is in `skip_params`."""


class _PackageImporter:
    """Finds the benchmark package only in its parent directory, as asv does,
    so a same-named package elsewhere on sys.path never shadows it."""

    def __init__(self, name, parent):
        self.name, self.parent = name, parent

    def find_spec(self, fullname, path, target=None):
        if fullname == self.name:
            return importlib.machinery.PathFinder().find_spec(fullname, [self.parent], target)
        return None


def use_package(benchmark_dir: str) -> str:
    """Make the benchmark directory importable as a package; its name."""
    benchmark_dir = os.path.abspath(benchmark_dir)
    name = os.path.basename(benchmark_dir)
    sys.meta_path.insert(0, _PackageImporter(name, os.path.dirname(benchmark_dir)))
    return name


def _first(sources, name, default=None):
    for source in sources:
        value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _all_ci(sources, name):
    """Every source's attribute named `name`, ignoring case, as asv finds setup and teardown."""
    found = []
    for source in sources:
        matches = [getattr(source, key) for key in dir(source) if key.lower() == name]
        if len(matches) > 1:
            raise ValueError(f"{getattr(source, '__name__', source)!r} has more than one {name}")
        if matches and matches[0] is not None:
            found.append(matches[0])
    return found


def _source(items) -> str:
    """asv's `code`: the benchmark's source, its setups' and its setup_cache's."""
    sources, previous = [], None
    for func in items:
        if hasattr(func, "pretty_source"):
            src = textwrap.dedent(func.pretty_source).lstrip()
        else:
            try:
                lines, _ = inspect.getsourcelines(func)
            except TypeError:
                continue
            if not lines:
                continue
            src = textwrap.dedent("\n".join(line.rstrip() for line in lines))
        class_name = None
        if inspect.ismethod(func) and "." in getattr(func, "__qualname__", ""):
            class_name = func.__qualname__.split(".")[-2]
        if class_name and previous != class_name:
            src = "class {}:\n    {}".format(class_name, src.replace("\n", "\n    "))
        elif class_name:
            src = "    " + src.replace("\n", "\n    ")
        sources.append(src)
        previous = class_name
    return "\n\n".join(sources).rstrip()


def _repr(obj) -> str:
    """repr() without a default `at 0x…` address, so the string is stable across runs."""
    text = repr(obj)
    match = re.match(r"^(<.*) at (0x[\da-fA-F]*)(>)$", text)
    if match:
        default = re.match(r"^(<.*) at (0x[\da-fA-F]*)(>)$", object.__repr__(obj))
        if default and default[2] == match[2]:
            text = match[1] + match[3]
    return text


def _params(params, param_names):
    params = list(params)
    if params and not isinstance(params[0], (tuple, list)):
        params = [params]
    else:
        params = [list(entry) for entry in params]
    param_names = [str(n) for n in param_names][:len(params)]
    param_names += [f"param{k + 1}" for k in range(len(param_names), len(params))]
    ids = []
    for entry in params:
        entry = [_repr(item) for item in entry]
        dupes = {n: 0 for n, count in Counter(entry).items() if count > 1}
        for j, n in enumerate(entry):
            if n in dupes:
                entry[j] = f"{n} ({dupes[n]})"
                dupes[n] += 1
        ids.append(entry)
    return params, param_names, ids


def _setup_cache_key(func):
    if func is None:
        return None
    module = inspect.getmodule(func)
    name = ".".join(module.__name__.split(".", 1)[1:]) or inspect.getsourcefile(func)
    return f"{name}:{inspect.getsourcelines(func)[1]}"


class Benchmark:
    """One benchmark as asv defines it: a function or a method of a fresh class instance."""

    def __init__(self, name, kind, func, sources, module, attr):
        self.name, self.type, self.func, self.module, self.attr = name, kind, func, module, attr
        self.sources = sources
        self.setups = _all_ci(sources, "setup")[::-1]
        self.teardowns = _all_ci(sources, "teardown")
        self.setup_cache = _first(sources, "setup_cache")
        self.setup_cache_key = _setup_cache_key(self.setup_cache)
        explicit = _first(sources, "version")
        code = _source([func] + self.setups + [self.setup_cache])
        self.version = str(explicit) if explicit is not None else hashlib.sha256(code.encode()).hexdigest()
        self.raw_params, self.param_names, self.params = _params(_first(sources, "params", []),
                                                                 _first(sources, "param_names", []))
        self.skip_params = _first(sources, "skip_params", [])
        self.declared = {k: _first(sources, k) for k in DECLARED if _first(sources, k) is not None}
        self.timer = _first(sources, "timer", time.perf_counter)
        self.current, self.cache, self._has_cache = (), None, False

    def describe(self) -> dict:
        """What the adapter needs to plan and record a case, as JSON."""
        declared = {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.declared.items()
                    if isinstance(v, (int, float, str, tuple, list))}
        return {"name": self.name, "type": self.type, "module": self.module, "attr": self.attr,
                "params": self.params, "param_names": self.param_names, "version": self.version,
                "setup_cache_key": self.setup_cache_key, "has_setup": bool(self.setups),
                "has_teardown": bool(self.teardowns), "declared": declared}

    def select(self, index):
        if index is not None:
            self.current = next(itertools.islice(itertools.product(*self.raw_params), index, index + 1))

    def set_cache(self, cache):
        self.cache, self._has_cache = cache, True

    def _args(self):
        return ((self.cache,) if self._has_cache else ()) + tuple(self.current)

    def setup(self):
        """Run the setups; Skipped when the case does not apply."""
        if tuple(self.current) in self.skip_params:
            raise Skipped("in skip_params")
        try:
            for setup in self.setups:
                setup(*self._args())
        except NotImplementedError as e:
            raise Skipped(repr(e)) from None

    def teardown(self):
        for teardown in self.teardowns:
            teardown(*self._args())

    def prepare_cache(self):
        """asv's setup_cache step: parameter-free setups, then setup_cache's value."""
        for setup in self.setups:
            if not inspect.ismethod(setup):
                try:
                    inspect.signature(setup).bind()
                except TypeError:
                    continue
                setup()
        return self.setup_cache()

    def time_func(self):
        """pyperf's time_func(loops): seconds for `loops` calls, the collector off as
        under asv. With setup hooks, `setup_scope` says how often they run, outside
        the timed region: "call" (the default, as asv_runner >= 0.3.0: asv#966,
        asv_runner#53) before every call, so every call sees freshly set-up state;
        "sample" (proposed in asv#1613) once per value, the calls of a value sharing it."""
        func, args, timer = self.func, self._args(), self.timer
        fresh = [True]  # the first value (or call) uses the setup the driver ran
        if not self.setups or self.declared.get("setup_scope", "call") == "sample":
            def run(loops):
                if self.setups and not fresh[0]:
                    self.teardown()
                    self.setup()
                fresh[0] = False
                enabled = gc.isenabled()
                gc.disable()
                try:
                    start = timer()
                    for _ in range(loops):
                        func(*args)
                    return timer() - start
                finally:
                    if enabled:
                        gc.enable()
            return run

        def run_isolated(loops):
            enabled = gc.isenabled()
            gc.disable()
            total = 0.0
            try:
                for _ in range(loops):
                    if not fresh[0]:
                        self.teardown()
                        self.setup()
                    fresh[0] = False
                    start = timer()
                    func(*args)
                    total += timer() - start
            finally:
                if enabled:
                    gc.enable()
            return total
        return run_isolated


def _benchmark(attr_name, module, klass, func):
    if getattr(func, "skip_benchmark", False):
        return None
    name = getattr(func, "benchmark_name", None)
    search = name.split(".")[-1] if name else attr_name
    kind = next((k for k, regex in TYPES if regex.match(search)), None)
    if kind is None:
        return None
    parts = module.__name__.split(".", 1)[1:]
    if klass is None:
        return Benchmark(name or ".".join(parts + [func.__name__]), kind, func, [func, module],
                         module.__name__, [attr_name])
    instance = klass()
    bound = getattr(instance, attr_name)
    return Benchmark(name or ".".join(parts + [klass.__name__, attr_name]), kind, bound,
                     [bound, instance, module], module.__name__, [klass.__name__, attr_name])


def _modules(name, failed):
    try:
        module = importlib.import_module(name)
    except BaseException as e:
        failed.append({"module": name, "error": f"{type(e).__name__}: {e}",
                       "traceback": traceback.format_exc()})
        return
    yield module
    for _, sub, _ in pkgutil.iter_modules(getattr(module, "__path__", None) or [], name + "."):
        yield from _modules(sub, failed)


def discover(benchmark_dir: str):
    """(benchmarks, failed imports), in asv's order."""
    package = use_package(benchmark_dir)
    failed, found = [], []
    for module in _modules(package, failed):
        for attr_name, value in list(module.__dict__.items()):
            if attr_name.startswith("_"):
                continue
            if inspect.isclass(value) and not inspect.isabstract(value):
                for name, member in inspect.getmembers(value):
                    if inspect.isfunction(member) or inspect.ismethod(member):
                        benchmark = _benchmark(name, module, value, member)
                        if benchmark is not None:
                            found.append(benchmark)
            elif inspect.isfunction(value):
                benchmark = _benchmark(attr_name, module, None, value)
                if benchmark is not None:
                    found.append(benchmark)
    return found, failed


def load(benchmark_dir: str, module: str, attr: list, index=None) -> Benchmark:
    """One benchmark by where discovery found it, without importing the rest of the suite."""
    use_package(benchmark_dir)
    mod = importlib.import_module(module)
    if len(attr) == 1:
        benchmark = _benchmark(attr[0], mod, None, getattr(mod, attr[0]))
    else:
        klass = getattr(mod, attr[0])
        benchmark = _benchmark(attr[1], mod, klass, getattr(klass, attr[1]))
    benchmark.select(index)
    return benchmark


def main(argv):
    """`list BENCHMARK_DIR`: the suite as JSON on the last line of stdout."""
    command, benchmark_dir = argv[:2]
    if command != "list":
        raise SystemExit(f"unknown command {command!r}")
    found, failed = discover(benchmark_dir)
    print(json.dumps({"benchmarks": [b.describe() for b in found], "failed": failed}))


if __name__ == "__main__":
    main(sys.argv[1:])
