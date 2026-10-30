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
    precision(flags, start) -> (the complete precision, the config file it
                            read or None): flags over its defaults
    describe_precision(precision, config_path) -> that precision in a few words
    suite_entry(suite, filter) -> one `suites[]` entry
    BARE_PATH_KIND          what a bare path names for this harness: a `build`
                            directory or a `working_tree`
    NAMES_TAKE_ARGUMENTS    whether a case name may be followed by `(arguments)`, one
                            combination of a parametrized benchmark; a bare name then
                            selects all of them (see `exact_filter`)
"""

import re

from . import config
from .errors import SessionError


def exact_filter(names, arguments=False) -> str | None:
    """Exact case names as one regex; None leaves the adapter's default case set.

    With `arguments`, a name may be followed by `(…)`: the filter is searched in
    `name(p0, p1)` for a parametrized benchmark, so the name alone then selects every
    combination and `name(10, 'a')` one, as `asv run --bench` does. The name stays
    exact: `time_sum` does not select `time_sum_fast`.
    """
    if not names:
        return None
    tail = r"(\(.*\))?" if arguments else ""
    return "^(" + "|".join(re.escape(n) for n in names) + ")" + tail + "$"


class Harness:
    name: str
    FLAGS: tuple[str, ...] = ()
    BARE_PATH_KIND: str
    NAMES_TAKE_ARGUMENTS = False

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

    def precision(self, flags: dict | None, start=".") -> tuple[dict, object]:
        raise NotImplementedError

    def describe_precision(self, precision: dict, config_path=None) -> str:
        raise NotImplementedError


class GoogleBenchmark(Harness):
    """Repetitions of one level; adaptive calibration by minimum time. No config
    file: Google Benchmark's defaults are left to the harness, except the
    repetition count, which the order always states."""

    name = "google-benchmark"
    FLAGS = ("repetitions", "min_time")
    BARE_PATH_KIND = "build"  # the binary is built; BUILD_DIR[:SOURCE_DIR]
    DEFAULT_REPETITIONS = 5

    def _precision_from(self, repetitions=None, min_time=None) -> dict:
        precision = {}
        if repetitions is not None:
            precision["repetitions"] = repetitions
        if min_time is not None:
            precision["calibration"] = {"mode": "adaptive", "minimum_sample_seconds": min_time}
        return precision

    def precision(self, flags, start="."):
        return {"repetitions": self.DEFAULT_REPETITIONS, **(flags or {})}, None

    def describe_precision(self, precision, config_path=None):
        return f"{precision['repetitions']} repetitions"


class Pyperf(Harness):
    """Two repetition levels (processes, values), calibration by loops or minimum
    time, and a warmup count. `.benchx/config.json` and pyperf's defaults fill
    what the flags leave out."""

    name = "pyperf"
    FLAGS = ("processes", "values", "min_time", "loops", "warmups")
    BARE_PATH_KIND = "working_tree"  # the module is run as found; the path is its own source
    NAMES_TAKE_ARGUMENTS = True  # a function with params is one case per combination

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

    def precision(self, flags, start="."):
        return config.resolve(self.name, flags, start=start)

    def describe_precision(self, precision, config_path=None):
        reps = {level["unit"]: level["n"] for level in precision["repetitions"]["levels"]}
        return f"{reps['process']} processes x {reps['value']} values (config: {config_path or 'none, pyperf defaults'})"


class Asv(Pyperf):
    """asv's benchmark format, timed by pyperf: pyperf's flags and config file.
    Only the process level has a default; what the flags leave out, each
    benchmark's own `repeat`, `number`, `sample_time` and `warmup_time` decide."""

    name = "asv"
    BARE_PATH_KIND = "working_tree"  # the suite is run as found, against the installed project

    def describe_precision(self, precision, config_path=None):
        reps = {level["unit"]: level["n"] for level in precision["repetitions"]["levels"]}
        return f"{reps['process']} rounds x {reps.get('value', 'per-benchmark')} repeat (config: {config_path or 'none'})"


GOOGLE_BENCHMARK = GoogleBenchmark()
PYPERF = Pyperf()
ASV = Asv()
HARNESSES = {h.name: h for h in (GOOGLE_BENCHMARK, PYPERF, ASV)}
# Every precision flag some harness takes, each once, in a stable order (for a CLI).
FLAG_NAMES = tuple(dict.fromkeys(name for h in HARNESSES.values() for name in h.FLAGS))


def get(name: str) -> Harness:
    """The harness registered under `name`; SessionError if there is none."""
    try:
        return HARNESSES[name]
    except KeyError:
        raise SessionError(f"no harness {name!r} ({', '.join(sorted(HARNESSES))})") from None
