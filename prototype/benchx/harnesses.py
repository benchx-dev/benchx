"""The order-side half of each harness: what the workbench needs to write a work order for it.

`benchx.adapters` is the runner's half (harness-adapter.md §1): it drives a
harness and translates its output, and the runner calls nothing else. Writing
the order is the workbench's job (bx bench, bx compare), so what differs by
harness there lives here instead, keyed by the same adapter name.

Each harness has
    name                    the adapter token, as written in `suites[].adapter`
    FLAGS                   the precision flags it takes, by keyword
    flags_to_precision(**flags) -> the flags spelled as a partial work-order
                            `precision`; a flag it does not take is refused
    precision(flags, file) -> the complete precision: flags over the config
                            file's precision over its defaults
    describe_precision(precision, config_path) -> that precision in a few words
    suite_entry(suite, filter) -> one `suites[]` entry
    BARE_PATH_KIND          what a bare path names for this harness: a `build`
                            directory or a `working_tree`
"""

import re

from . import adapters, config, core
from .errors import SessionError


def exact_filter(names) -> str | None:
    """Exact case names as one regex; None leaves the adapter's default case set."""
    return "^(" + "|".join(re.escape(n) for n in names) + ")$" if names else None


class Harness:
    name: str
    FLAGS: tuple[str, ...] = ()
    BARE_PATH_KIND: str

    def suite_entry(self, suite: str, filter: str | None = None) -> dict:
        entry = {"adapter": self.name, "suite": suite}
        if filter:
            entry["filter"] = filter
        return entry

    def flags_to_precision(self, **flags) -> dict:
        given = {name: value for name, value in flags.items() if value is not None}
        if extra := sorted(set(given) - set(self.FLAGS)):
            spelled = ", ".join("--" + name.replace("_", "-") for name in extra)
            raise SessionError(f"{self.name} takes no {spelled}")
        return self._precision_from(**given)

    def _precision_from(self, **flags) -> dict:
        raise NotImplementedError

    def precision(self, flags: dict | None, file: dict | None = None) -> dict:
        """The complete precision; ConfigError if the result is not a valid one."""
        merged = config.merge(config.merge(self._defaults(), file or {}), flags or {})
        if errors := core.check_precision(merged):
            raise config.ConfigError("precision: " + "; ".join(str(error) for error in errors))
        return merged

    def _defaults(self) -> dict:
        return adapters.get(self.name).default_precision()

    def describe_precision(self, precision: dict, config_path=None) -> str:
        raise NotImplementedError


class GoogleBenchmark(Harness):
    """Repetitions of one level; adaptive calibration by minimum time, and a
    time-based warmup. Whatever the config file and the flags leave out takes
    Google Benchmark's default, and the order states it."""

    name = "google-benchmark"
    FLAGS = ("repetitions", "min_time")
    BARE_PATH_KIND = "build"  # the binary is built; BUILD_DIR[:SOURCE_DIR]
    DEFAULT_REPETITIONS = adapters.gbench.DEFAULT_REPETITIONS

    def _precision_from(self, repetitions=None, min_time=None) -> dict:
        precision = {}
        if repetitions is not None:
            precision["repetitions"] = repetitions
        if min_time is not None:
            precision["calibration"] = {"mode": "adaptive", "minimum_sample_seconds": min_time}
        return precision

    def describe_precision(self, precision, config_path=None):
        return f"{precision['repetitions']} repetitions"


class Pyperf(Harness):
    """Two repetition levels (processes, values), calibration by loops or minimum
    time, and a warmup count. `benchx.conf.json` and pyperf's defaults fill what the
    flags leave out."""

    name = "pyperf"
    FLAGS = ("processes", "values", "min_time", "loops", "warmups")
    BARE_PATH_KIND = "working_tree"  # the module is run as found; the path is its own source

    def _precision_from(self, processes=None, values=None, min_time=None, loops=None, warmups=None) -> dict:
        if min_time is not None and loops is not None:
            raise SessionError("--min-time and --loops both set calibration; give one")
        precision = {}
        levels = [{"unit": unit, "n": n} for unit, n in (("process", processes), ("value", values)) if n is not None]
        if levels:
            precision["repetitions"] = {"mode": "fixed", "levels": levels}
        if min_time is not None:
            precision["calibration"] = {"mode": "adaptive", "minimum_sample_seconds": min_time}
        if loops is not None:
            precision["calibration"] = {"mode": "fixed", "n_iterations": loops}
        if warmups is not None:
            precision["warmup"] = {"mode": "count", "n_warmup": warmups} if warmups else {"mode": "none"}
        return precision

    def describe_precision(self, precision, config_path=None):
        reps = {level["unit"]: level["n"] for level in precision["repetitions"]["levels"]}
        return f"{reps['process']} processes x {reps['value']} values (config: {config_path or 'none, pyperf defaults'})"


GOOGLE_BENCHMARK = GoogleBenchmark()
PYPERF = Pyperf()
HARNESSES = {h.name: h for h in (GOOGLE_BENCHMARK, PYPERF)}
# Every precision flag some harness takes, each once, in a stable order (for a CLI).
FLAG_NAMES = tuple(dict.fromkeys(name for h in HARNESSES.values() for name in h.FLAGS))


def get(name: str) -> Harness:
    """The harness registered under `name`; SessionError if there is none."""
    try:
        return HARNESSES[name]
    except KeyError:
        raise SessionError(f"no harness {name!r} ({', '.join(sorted(HARNESSES))})") from None
