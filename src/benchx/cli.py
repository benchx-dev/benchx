"""bx: a thin command line over the benchx package (prototype-design.md §1).

Every command reads the project's `benchx.conf.json` (benchx.config), found from the
working directory upward. The local store and the result files are in the
project root unless that file says otherwise, or `$BENCHX_HOME` relocates the store. A command-line argument wins over
the file, and the file over a built-in default.
"""

import argparse
import dataclasses
import json
import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import get_args

from . import bench as bench_mod
from . import compare as compare_mod
from . import config as config_mod
from . import core, harnesses, log, orders, runner, session, snapshot
from . import tables as tables_mod
from . import target as target_mod
from . import views as views_mod
from .core.validation import Kind
from .results import MemoryStore, warning_codes

EXIT_OK = 0
EXIT_INVALID = 1  # a document invalid, a result rejected, or a target without a description
EXIT_REFUSED = 2  # the command was refused or could not be carried out
ASV_CASE_TIMEOUT_S = 1800
VERBOSE_ENV = "BENCHX_VERBOSE"
LOG_LEVEL_ENV = "BENCHX_LOG_LEVEL"
HARNESS_VERBOSITY_ENV = "BENCHX_HARNESS_VERBOSITY"
DEFAULT_LOG = "{out}/bx.log"
# benchx's own control variables: every one the code reads. They are settings someone typed for benchx, a fixed
# list, so the log gives their values; the rest of the inherited environment is never dumped.
CONTROL_ENV = (
    "BENCHX_HOME",
    "BENCHX_VERBOSE",
    "BENCHX_LOG_LEVEL",
    "BENCHX_HARNESS_VERBOSITY",
    "BENCHX_SETUP_FILE",
    "BENCHX_ENV_ALLOWLIST",
    "BENCHX_SCHEMAS",
)
OUTCOMES = {EXIT_OK: "ok", EXIT_INVALID: "invalid", EXIT_REFUSED: "refused"}

say = log.console(__name__)  # what `bx` shows its user: console messages
logger = logging.getLogger(__name__)


def _refused(error: runner.Refused) -> int:
    for problem in error.problems:
        say.error(f"refused: {problem}")
    return EXIT_REFUSED


def _load(command):
    """The project's configuration, or None (after saying why) when it is unusable.
    The store and the results directory are left out of every tree identity."""
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        say.error(f"bx {command}: error: {e}")
        return None
    snapshot.ignore(*cfg.own_paths)
    return cfg


def _source_text(source, env=None) -> str:
    return f"environment {env}" if source == "env" else source


def _verbosity(args) -> tuple[int, str, int]:
    """How much the terminal shows, -2 (`-qq`) to 2 (`-vv`), where that came from, and how many `v` there are
    beyond two: the net count of `-v` and `-q`, else $BENCHX_VERBOSE, else 0. Each one past `-vv` asks the harness
    to say more (`-vvv` is `-vv` and --harness-verbosity 1). UsageError for a bad variable."""
    if args.verbose is not None or args.quiet is not None:
        net, source = (args.verbose or 0) - (args.quiet or 0), "flag"
    else:
        net, source = 0, "default"
        if text := os.environ.get(VERBOSE_ENV):
            try:
                net = int(text)
            except ValueError:
                net = None
            if net is None or net < -2:
                raise UsageError(f"{VERBOSE_ENV}={text}: not a whole number, -2 or more")
            source = _source_text("env", VERBOSE_ENV)
    return max(-2, min(2, net)), source, max(0, net - 2)


def _non_negative(text) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number from 0")
    return value


def _harness_verbosity(cfg, args) -> tuple[int, str]:
    """How much the harness is asked to say, and where that came from: `--harness-verbosity`, else the `v`s past
    `-vv` on the command line, else $BENCHX_HARNESS_VERBOSITY, else the `v`s past `-vv` that $BENCHX_VERBOSE
    stands for, else `adapter.verbosity` in the file, else 0 (nothing more). Only the flags beat the
    environment: $BENCHX_VERBOSE does not override an explicit $BENCHX_HARNESS_VERBOSITY."""
    given = getattr(args, "harness_verbosity", None)  # `bx config show` has no such option
    extra = getattr(args, "verbose_extra", 0)
    from_flag = getattr(args, "verbosity_source", "flag") == "flag"
    label = None
    if given is None and extra and from_flag:
        given, label = extra, "flag: -v past -vv"
    value, source = cfg.pick_with_source(
        given,
        cfg.adapter.get("verbosity"),
        0,
        flag="--harness-verbosity",
        key="adapter.verbosity",
        env=HARNESS_VERBOSITY_ENV,
    )
    if source == "env":
        try:
            value = int(value)
        except ValueError:
            value = -1
        if value < 0:
            raise UsageError(f"{HARNESS_VERBOSITY_ENV}={os.environ[HARNESS_VERBOSITY_ENV]}: not a whole number from 0")
    elif extra and not from_flag and source in ("file", "default"):
        return extra, _source_text("env", VERBOSE_ENV)  # the environment's, which beats the file
    return value, label if source == "flag" and label else _source_text(source, HARNESS_VERBOSITY_ENV)


def _log_level(cfg, args) -> tuple[int, str]:
    """The level the log keeps, and where it came from: the flag, $BENCHX_LOG_LEVEL, the file's
    `log_level`, else debug."""
    value, source = cfg.pick_with_source(
        args.log_level, cfg.data.get("log_level"), "debug", flag="--log-level", key="log_level", env=LOG_LEVEL_ENV
    )
    if value.lower() not in log.LEVELS:
        origin = f"{LOG_LEVEL_ENV}={value}" if source == "env" else f"log_level in {cfg.path}"
        raise UsageError(f"{origin}: not one of {', '.join(log.LEVELS)}")
    return log.LEVELS[value.lower()], _source_text(source, LOG_LEVEL_ENV)


def _log_setting(cfg, args, out, command):
    """What the switch says for this command, and where that came from: False (off), True
    (stderr) or the resolved log file. A command with no output folder (`out` None) logs only
    when a flag asks: the file, the environment and the default are ignored."""
    setting, source = cfg.pick_with_source(args.log, cfg.data.get("logging"), DEFAULT_LOG, flag="--log", key="logging")
    if out is None and source != "flag":
        return False, source
    if isinstance(setting, str):
        setting = setting.replace("{root}", str(cfg.root))
        if "{out}" in setting:
            if out is None:
                raise UsageError(f"--log-file {setting}: bx {command} has no output folder")
            setting = Path(setting.replace("{out}", str(out))).resolve()
        elif source == "file":
            setting = cfg._path(setting).resolve()  # relative to the project root, as paths.store is
        else:
            setting = Path(setting).expanduser().resolve()  # a flag: relative to the working directory
    return setting, source


def _check_log_file(path, out, origin):
    """UsageError unless `path` can be appended to. benchx creates only its own output folder."""
    if str(path).endswith(".json"):
        raise UsageError(f"{origin}: a log file must not end in .json")
    folder = Path(out).resolve() if out is not None else None
    if path == folder or path.is_dir():
        raise UsageError(f"{origin}: {path} is a directory")
    try:
        # Only the output folder itself is created, and only for a file directly in it or in a folder that exists:
        # a refused path leaves nothing behind.
        if folder is not None and path.is_relative_to(folder) and (path.parent == folder or path.parent.is_dir()):
            folder.mkdir(parents=True, exist_ok=True)
        path.open("a").close()
    except OSError as e:
        raise UsageError(f"{origin}: cannot open {path}: {e}") from None


def _start_logging(cfg, args, out, command, *, read_config=True, lenient=False):
    """Turn the log on for this command: the config with `log_file` set, or None (after saying
    why) when the setting is unusable. Called once the command has validated its own flags and
    values, so a refusal for those leaves no folder, and before it calls `session`.
    `out` is the command's output folder, or None. `lenient`: a log that cannot be opened is
    skipped without a word, unless a flag named it (a read must not need to write)."""
    try:
        if args.log is False and args.log_level is not None:
            raise UsageError("--no-log with --log-level: there is nothing to set a level on")
        level, level_source = _log_level(cfg, args)
        setting, source = _log_setting(cfg, args, out, command)
        if isinstance(setting, Path):
            origin = {"flag": f"--log-file {args.log}", "file": f"logging in {cfg.path}"}.get(source, "the default log")
            try:
                _check_log_file(setting, out, origin)
            except UsageError:
                if not (lenient and source != "flag"):
                    raise
                setting = False
    except UsageError as e:
        say.error(f"bx {command}: error: {e}")
        return None
    log.configure(setting, level=level, argv=args.argv)
    cfg = dataclasses.replace(cfg, log_file=setting if isinstance(setting, Path) else None)
    snapshot.ignore(*cfg.own_paths, *([args.out] if getattr(args, "out", None) else []))  # the log is not the tree
    if read_config:
        _log_config(cfg)
    _log_arguments(args)
    _log_environment()
    shown = "off" if setting is False else "stderr" if setting is True else str(setting)
    logger.debug(
        "logging: %s (%s); log level: %s (%s); verbosity: %d (%s)",
        shown,
        _source_text(source),
        logging.getLevelName(level).lower(),
        level_source,
        args.verbosity,
        args.verbosity_source,
    )
    return cfg


def _log_arguments(args):
    """The arguments as argparse understood them (DEBUG), beside the command line as given in the header: a
    prefix spelled out, a case name taken from after the options, an option's value. As given: the log makes no
    claim to hide a value."""
    skip = {"fn", "argv", "verbosity", "verbosity_source", "verbose_extra"}
    # `0 == False`, so a test with `in` would drop `--slot 0`: compare by identity
    shown = {
        name: value
        for name, value in vars(args).items()
        if name not in skip and value is not None and value is not False and value != [] and value != ()
    }
    logger.debug("arguments as parsed: %s", ", ".join(f"{name}={value!r}" for name, value in sorted(shown.items())))


def _log_environment():
    """The environment variables `bx` consults that decide what it does (DEBUG): benchx's own, with their values,
    and whether PATH is set, which the benchmark's `python` is looked up on. The other variables that shape a
    run are the allowlisted ones, which each order logs by name."""
    shown = [f"{name}={os.environ[name]}" if os.environ.get(name) else f"{name} not set" for name in CONTROL_ENV]
    logger.debug(
        "environment variables consulted: %s; PATH is %s",
        "; ".join(shown),
        "set (the benchmark's python is looked up on it)" if os.environ.get("PATH") else "not set",
    )


def _log_config(cfg):
    """Which config file was used, and where the store and the results are (INFO, once)."""
    found = cfg.path or f"none found from {Path.cwd()} upward"
    home = ""
    if os.environ.get(config_mod.HOME_ENV):
        home = f" (set by ${config_mod.HOME_ENV}" + (
            ", which hides paths.store in the file)" if "store" in cfg.section("paths") else ")"
        )
    logger.info(
        "config file: %s; project root %s; store %s%s; results %s",
        found,
        cfg.root,
        cfg.store_path,
        home,
        cfg.results_dir,
    )


def _log_settings(sources):
    """The settings a command resolved, each with its value and where it came from (DEBUG)."""
    for name, value, source in sources:
        # the project decides where the results are filed: worth a line at INFO
        logger.log(logging.INFO if name == "project" else logging.DEBUG, "setting %s: %s (%s)", name, value, source)


def _report_unmeasured(rows) -> None:
    """What a run says about the cases it did not measure, for `bx run` and `bx bench` (the table is the
    caller's). The section under the table, `case: message`, is an answer when a case failed (WARNING, shown at
    every verbosity). When only skipped cases said anything, which the run expects, it is INFO and goes with the
    table. The exit status stays 0 either way: `degraded` puts a phrase in the log's footer for the cases that
    failed, and on stdout too when what the reader would see leaves some out (a hidden table, or an error that
    said nothing)."""
    said = bench_mod.failures(rows)
    errors = [row for row in rows if not row["status"].startswith(("success", "skipped"))]
    named = {row["case"] for row in errors if row["message"]}
    if said:
        loud = bool(named)
        say.out(said, log.ANSWER if loud else log.TABLE, level=logging.WARNING if loud else logging.INFO)
    unmeasured = {row["case"] for row in errors}
    if unmeasured:
        cases = {row["case"] for row in rows}
        silent = unmeasured - named  # nothing on the screen names these, whatever else is shown
        log.degraded(
            f"{len(unmeasured)} of {len(cases)} cases not measured", shown=bool(log.quiet() and (not named or silent))
        )


def _open_store(path, command: str, *, hint: str | None = None):
    """Open the local store (Parquet or JSONL), or explain how to install pyarrow for a Parquet file."""
    from .store import Store

    try:
        return Store(path)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "pyarrow":
            suffix = f", or {hint}" if hint else ""
            say.error(
                f"bx {command}: error: Parquet store ({path}) requires pyarrow; "
                f"install it with `pip install 'benchx[parquet]'`{suffix}"
            )
            return None
        raise


def cmd_run(args):
    """Run one order, write its result files, and deliver them to the local
    store. The runner itself stays store-unaware (runner.md §2 principle 6);
    this command composes it with ingest, as the workbench does."""
    if (cfg := _load("run")) is None:
        return EXIT_REFUSED
    store = None
    if not args.no_ingest:
        store = _open_store(cfg.store_path, "run", hint="pass --no-ingest to write JSON results only")
        if store is None:
            return EXIT_REFUSED
    out = Path(args.out) if args.out else cfg.results_dir
    # `bx run` has no check phase; the log goes in --out, and nowhere without it (the results
    # directory is shared by every run).
    if (cfg := _start_logging(cfg, args, Path(args.out) if args.out else None, "run")) is None:
        return EXIT_REFUSED
    logger.info("order file %s; output %s", Path(args.order).resolve(), out)
    say.progress(f"running the order {Path(args.order).name}")
    try:
        summary = runner.run(args.order, out, allow_collectors=args.allow_collectors)
    except runner.Refused as e:
        return _refused(e)
    _report_unmeasured(bench_mod.summarize(summary["files"]))
    say.out(
        f"{summary['results']} result(s) for {summary['cases']} case(s) -> {summary['out']}"
        f"  (order {summary['order'][:19]})",
        log.SUMMARY,
    )
    if store is None:
        logger.info("ingest skipped (--no-ingest): the results are files only")
        return EXIT_OK
    return _report(store.ingest_paths(summary["files"]), store.path)


def _environment(items) -> dict:
    """`-e NAME=VALUE`, repeated, as a mapping; UsageError on a malformed item."""
    env = {}
    for item in items or []:
        name, eq, value = item.partition("=")
        if not (name and eq):
            # Not echoed: a value typed without its NAME, or after an empty one, is a secret more
            # often than a typo, and this message goes to stderr and to the log.
            raise UsageError("-e wants NAME=VALUE (the item is not shown, in case it holds a value)")
        env[name] = value
    return env


def _parse_round(text) -> tuple[int | None, int | None]:
    """`--round R` or `--round R/N`: the round, numbered from 0, and how many rounds the session has when the
    caller says (only the start line shows it: the work order records the round alone). UsageError."""
    if text is None:
        return None, None
    first, slash, last = text.partition("/")
    try:
        round_, rounds = int(first), int(last) if slash else None
    except ValueError:
        raise UsageError(f"--round wants R, or R/N for a session of N rounds, whole numbers, not {text!r}") from None
    if round_ < 0:
        raise UsageError(f"--round wants a number from 0, not {round_}")
    if rounds is not None and rounds < 1:
        raise UsageError(f"--round {text}: a session has at least 1 round, not {rounds}")
    if rounds is not None and not round_ < rounds:
        raise UsageError(f"--round {text}: with {rounds} round(s) they are numbered 0 to {rounds - 1}")
    return round_, rounds


def _session_position(args) -> tuple[dict, int | None, int | None, int | None]:
    """Where `bx bench` sits in a session that other calls join through the same `--run-key`: its
    `--label NAME=VALUE`s, `--round` (and, with `--round R/N`, the session's number of rounds) and `--slot`.
    UsageError when they could not join one."""
    labels = {}
    for item in args.label or []:
        name, eq, value = item.partition("=")
        if not (name and eq and value):
            raise UsageError(f"--label wants NAME=VALUE, not {item!r}")
        session.check_label_name(name)  # a SessionError, which cmd_bench reports as it does a UsageError
        if name in labels:
            raise UsageError(f"--label {name} is given twice")
        labels[name] = value
    round_, rounds = _parse_round(args.round)
    if args.slot is not None and args.slot < 0:
        raise UsageError(f"--slot wants a number from 0, not {args.slot}")
    placed = labels or round_ is not None or args.slot is not None
    if placed and not args.run_key:
        raise UsageError(
            "--label, --round and --slot need an explicit --run-key: the default is a timestamp "
            "per call, so two calls would never share a run"
        )
    if (labels or round_ is not None) and args.slot is None:
        raise UsageError(
            "--label and --round need --slot: without one, the sides of a run would share an "
            "ingest key and their results would conflict"
        )
    return labels, round_, args.slot, rounds


def _bench_target(args) -> tuple[str, str | None, str | None]:
    """The target, the suite it forces (or None: --suite, then the file's), and the
    adapter it forces. With `--suite` the target is as given. Without it, the target
    is a Python module (the shorthand `bx bench MODULE.py [FUNCTION ...]`): its
    checkout is the target, its path in it the suite, and the harness pyperf. With
    `--adapter asv` it is an asv suite instead (`bx bench PATH --adapter asv`): PATH
    holds asv.conf.json or is the benchmark directory."""
    if not args.suite and args.adapter == "asv":
        logger.debug("the asv shorthand: --adapter asv on the command line, and no --suite")
        return (*bench_mod.asv_target(args.target, args.tree), "asv")
    if args.suite or not args.target.endswith(".py"):
        if args.tree:
            raise UsageError("--tree belongs to the shorthands; give the tree as the TARGET with --suite")
        logger.debug("a plain target: %s, with the suite from --suite or the file's adapter.suite", args.target)
        return args.target, None, None
    if args.adapter not in (None, "pyperf"):
        raise UsageError(f"a Python file is benchmarked with pyperf, not {args.adapter}")
    logger.debug("the Python-module shorthand: %s is benchmarked with pyperf", args.target)
    target, suite = bench_mod.module_target(args.target, args.tree)
    return target, suite, "pyperf"


def _common(cfg, args, *, forced_adapter=None, forced_suite=None) -> dict:
    """The settings `bx bench` and `bx compare` share, each from the command line,
    else the file, else its default (config.Config.pick); ConfigError when a
    required one has none. The file's adapter block belongs to the adapter it
    names and to the others of its family (pyperf's serves asv), so naming an
    adapter of another family is refused. `sources` lists each setting with where it came
    from, for the log (the caller pops it before the rest goes to `session`)."""
    block = cfg.adapter
    given = forced_adapter or args.adapter
    if given and block.get("name") and not harnesses.same_family(given, block["name"]):
        raise config_mod.ConfigError(
            f"adapter {given!r}, but {cfg.where} sets adapter.name to {block['name']!r}, "
            "and its precision belongs to that harness and the ones that share it "
            "(pyperf and asv)"
        )
    sources = []
    quantities = config_mod.parse_quantities(args.quantity) if args.quantity else block.get("quantities", ["wall-time"])
    sources.append(
        (
            "quantities",
            ", ".join(quantities),
            "flag" if args.quantity else "file" if "quantities" in block else "default",
        )
    )
    if args.project and args.no_project:
        raise UsageError("give --project or --no-project, not both")
    timeouts = cfg.section("timeouts")
    adapter, adapter_source = cfg.pick_with_source(given, block.get("name"), flag="--adapter", key="adapter.name")
    named = block.get("name")
    if given and named and given != named:  # the file's block serves another adapter of its family
        adapter_source += f"; the file says {named}, same family"
    sources.append(("adapter", adapter, adapter_source))
    suite, suite_source = cfg.pick_with_source(
        forced_suite or args.suite, block.get("suite"), flag="--suite", key="adapter.suite"
    )
    sources.append(("suite", suite, suite_source))
    from_flag, from_file = _environment(args.env), cfg.section("environment_variables")
    environment_variables = {**from_file, **from_flag}
    where = [
        f"-e: {', '.join(sorted(from_flag))}" if from_flag else "",
        f"file: {', '.join(sorted(from_file))}" if from_file else "",
    ]
    if overrode := sorted(set(from_flag) & set(from_file)):
        where.append(f"-e overrode the file's {', '.join(overrode)}")
    sources.append(("environment_variables", "names only", "; ".join(w for w in where if w) or "default: none"))
    if args.no_project:
        project = None
        sources.append(("project", "none: results are filed as local/HOST", "flag --no-project"))
    else:
        project, project_source = cfg.pick_with_source(
            args.project, cfg.data.get("project"), None, flag="--project", key="project"
        )
        sources.append(("project", project or "none: results are filed as local/HOST", project_source))
    source_uri, uri_source = cfg.pick_with_source(
        args.source_uri or None, cfg.section("source").get("uri"), None, flag="--source-uri", key="source.uri"
    )
    sources.append(("source.uri", source_uri or "the checkout's origin remote", uri_source))
    case_s, case_source = cfg.pick_with_source(
        getattr(args, "case_timeout", None),
        timeouts.get("case_s"),
        ASV_CASE_TIMEOUT_S if adapter == "asv" else orders.CASE_TIMEOUT_S,
        flag="--case-timeout",
        key="timeouts.case_s",
    )
    if case_source == "default":
        case_source = "default for asv, which times every round of a benchmark" if adapter == "asv" else "default"
    sources.append(("timeouts.case_s", f"{case_s:g} s", case_source))
    order_s = timeouts.get("order_s", orders.ORDER_TIMEOUT_S)
    sources.append(("timeouts.order_s", f"{order_s:g} s", "file" if "order_s" in timeouts else "default"))
    flagged = [name for name in harnesses.FLAG_NAMES if getattr(args, name) is not None]
    precision_file = sorted(block.get("precision") or {})
    sources.append(
        (
            "precision",
            f"flags {', '.join(flagged) or 'none'}; file {', '.join(precision_file) or 'none'}",
            "flag over file over the harness's defaults",
        )
    )
    if args.filter:
        sources.append(("filter", args.filter, "flag"))
    harness_verbosity, verbosity_source = _harness_verbosity(cfg, args)
    sources.append(("harness verbosity", harness_verbosity, verbosity_source))
    flags = {name: getattr(args, name) for name in harnesses.FLAG_NAMES}
    harnesses.get(adapter).flags_to_precision(**flags)
    return {
        "adapter": adapter,
        "suite": suite,
        "flags": flags,
        "file_precision": block.get("precision"),
        "quantities": quantities,
        "filter": args.filter,
        "environment_variables": environment_variables,
        "project": project,
        "run_key": args.run_key,
        "out": args.out,
        "source_uri": source_uri,
        "build": not args.no_build,
        "provider": cfg.provider,
        "collectors": cfg.collectors,
        "results_dir": cfg.results_dir,
        # An asv case is every round of its benchmark, each bounded by the benchmark's own timeout.
        "case_s": case_s,
        "order_s": order_s,
        "config_path": cfg.path,
        "harness_verbosity": harness_verbosity,
        "sources": sources,
    }


def _needs_provider(cfg, *targets):
    for spec in targets:
        if spec.startswith("@"):
            cfg.require_provider(spec)


def _placed(settings, rounds=None) -> str:
    """` (round 1/5, slot 3)`: where a `bx bench` call sits in a session, as the start line says it, with the
    session's number of rounds when the caller gave it (`--round 1/5`); empty when alone."""
    parts = [
        f"round {settings['round_']}{f'/{rounds}' if rounds else ''}" if settings["round_"] is not None else "",
        f"slot {settings['slot']}" if settings["slot"] is not None else "",
        ", ".join(f"{name}={value}" for name, value in (settings["labels"] or {}).items()),
    ]
    return f" ({', '.join(part for part in parts if part)})" if any(parts) else ""


def cmd_bench(args):
    """Run one target once, as one order of a comparison session: resolve the target,
    build the order from the harness defaults, config file, flags and environment
    variables, run it, deliver, print a table."""
    if (cfg := _load("bench")) is None:
        return EXIT_REFUSED
    try:
        if args.functions and args.filter:
            raise UsageError("give case names or --filter, not both")
        target, forced_suite, forced_adapter = _bench_target(args)
        settings = _common(cfg, args, forced_adapter=forced_adapter, forced_suite=forced_suite)
        settings["labels"], settings["round_"], settings["slot"], rounds = _session_position(args)
        session.check_harness(
            settings["adapter"], settings["flags"], settings["file_precision"], settings["quantities"]
        )
        _needs_provider(cfg, target)
        # A name may carry `(arguments)` where the harness has parametrized benchmarks.
        names = harnesses.exact_filter(args.functions, harnesses.get(settings["adapter"]).NAMES_TAKE_ARGUMENTS)
    except (UsageError, config_mod.ConfigError, bench_mod.SessionError) as e:
        say.error(f"bx bench: error: {e}")
        return EXIT_REFUSED
    # The run key and the output folder are settled before any target is resolved (a provider
    # may build), so the log can live in the folder from the start.
    run_key = args.run_key or orders.default_run_key("bench")
    out = Path(args.out) if args.out else session.run_directory(cfg.results_dir, run_key)
    if (cfg := _start_logging(cfg, args, out, "bench")) is None:
        return EXIT_REFUSED
    logger.info(
        "run key %s (%s); output folder %s (%s)",
        run_key,
        "given" if args.run_key else "generated",
        out,
        "--out" if args.out else "the results directory",
    )
    _log_settings(settings.pop("sources"))
    settings["run_key"], settings["out"] = run_key, out
    settings["filter"] = names or settings["filter"]
    store = None
    if not args.no_ingest:
        store = _open_store(cfg.store_path, "bench", hint="pass --no-ingest to write JSON results only")
        if store is None:
            return EXIT_REFUSED
    # Where a quiet run says it started, before a provider that builds takes its time.
    say.progress(
        f"measuring {Path(settings['suite']).name if settings.get('suite') else 'the suite'} on {target}"
        + _placed(settings, rounds)
    )
    try:
        summary = session.bench_target(target, store=store, progress=say.info, **settings)
    except (session.SessionError, config_mod.ConfigError) as e:
        say.error(f"bx bench: error: {e}")
        return EXIT_REFUSED
    except runner.Refused as e:
        return _refused(e)
    # What the run reports goes through the marker: stdout, by the tiers `-q` and `-qq` leave, and the log.
    rows = bench_mod.summarize(summary["files"])
    say.out(bench_mod.render(rows), log.TABLE)
    _report_unmeasured(rows)
    say.out(f"\n{summary['results']} result(s) -> {summary['out']}  (order {summary['order'][:19]})", log.SUMMARY)
    if store is None:
        logger.info("ingest skipped (--no-ingest): the results are files only")
        return EXIT_OK
    return _report(summary["outcome"], store.path)


def cmd_config(args):
    if args.action == "init":
        # `init` does not read the file: it resolves the logging settings from the flags and the environment
        if _start_logging(_no_config(), args, None, "config", read_config=False) is None:
            return EXIT_REFUSED
        try:
            path = config_mod.write_template(args.dir, args.adapter)
        except config_mod.ConfigError as e:
            say.error(f"bx config: error: {e}")
            return EXIT_REFUSED
        print(f"wrote {path}")
        return EXIT_OK
    if (cfg := _load("config")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "config")) is None:
        return EXIT_REFUSED
    try:  # all the rows first: a bad $BENCHX_HARNESS_VERBOSITY refuses the command, with nothing half printed
        rows = [*_resolved(cfg), *_logging_rows(cfg, args)]
    except UsageError as e:
        say.error(f"bx config: error: {e}")
        return EXIT_REFUSED
    print(f"file  {cfg.where}\nroot  {cfg.root}")
    for key, value, source in rows:
        print(f"{key:<26} {value}  ({source})")
    return EXIT_OK


def _no_config() -> config_mod.Config:
    """The configuration of a command that does not read the file: the working directory, no data."""
    return config_mod.Config(root=Path.cwd(), path=None, data={})


def _logging_rows(cfg, args):
    """What `bx config show` adds for the log: the switch, the level and the verbosity, each with
    where it came from (a flag, an environment variable, the file or the default)."""
    setting, source = cfg.pick_with_source(args.log, cfg.data.get("logging"), DEFAULT_LOG, flag="--log", key="logging")
    if isinstance(setting, str):
        setting = setting.replace("{root}", str(cfg.root))
    if isinstance(setting, str) and "{out}" not in setting:
        setting = str(cfg._path(setting).resolve() if source == "file" else Path(setting).expanduser().resolve())
    else:
        setting = json.dumps(setting)
    yield "logging", setting, _source_text(source)
    level, level_source = _log_level(cfg, args)
    yield "log level", logging.getLevelName(level).lower(), level_source
    yield "verbosity", str(args.verbosity), args.verbosity_source
    harness, harness_source = _harness_verbosity(cfg, args)
    yield "harness verbosity", str(harness), harness_source


def _resolved(cfg):
    """What the file and the built-in defaults give, before any command-line argument:
    (key, value, 'file' | 'default' | 'unset')."""

    def row(key, value, default=None):
        if value is not None:
            return key, json.dumps(value), "file"
        return key, json.dumps(default) if default is not None else "-", "default" if default is not None else "unset"

    block, timeouts, compare = cfg.adapter, cfg.section("timeouts"), cfg.section("compare")
    yield row("project", cfg.data.get("project"))
    yield row("source.uri", cfg.section("source").get("uri"))
    yield row("provider.command", cfg.section("provider").get("command"))
    yield row("adapter.name", block.get("name"))
    yield row("adapter.suite", block.get("suite"))
    yield row("adapter.quantities", block.get("quantities"), ["wall-time"])
    if block.get("name"):
        resolved = harnesses.get(block["name"]).precision({}, block.get("precision"))
        yield "adapter.precision", json.dumps(resolved), "file + default" if block.get("precision") else "default"
    yield row("environment_variables", cfg.data.get("environment_variables"), {})
    yield row("timeouts.case_s", timeouts.get("case_s"), orders.CASE_TIMEOUT_S)
    yield row("timeouts.order_s", timeouts.get("order_s"), orders.ORDER_TIMEOUT_S)
    store_from = (
        "$BENCHX_HOME"
        if os.environ.get(config_mod.HOME_ENV)
        else "file"
        if "store" in cfg.section("paths")
        else "default"
    )
    yield "paths.store", str(cfg.store_path), store_from
    yield "paths.results", str(cfg.results_dir), "file" if "results" in cfg.section("paths") else "default"
    yield row("compare.profile", compare.get("profile"))
    yield row("compare.rounds", compare.get("rounds"), session.ROUNDS)
    yield row("compare.k", compare.get("k"), 3.0)
    yield row("compare.min_rounds", compare.get("min_rounds"), 4)


def _report(outcome, store_path=None):
    """The ingest counts. After a run (`store_path` names the store) they are a progress message,
    `STORE: ingested 3, duplicate 0`, on stderr as every progress message is: `-q` does not show it. For the
    command that ingests they are its answer, on stdout, whatever the verbosity."""
    for r in outcome["rejected"]:
        say.warning(f"  rejected {r['document']}: {r['code']} — {r['message']}")
    counts = f"ingested {outcome['ingested']}, duplicate {outcome['duplicate']}"
    if outcome["rejected"]:
        codes = sorted({r["code"] for r in outcome["rejected"]})
        counts += f", rejected {len(outcome['rejected'])} ({', '.join(codes)})"
    if store_path:
        say.info(f"{store_path}: {counts}")
    else:
        say.out(counts, log.ANSWER)
    return EXIT_INVALID if outcome["rejected"] else EXIT_OK


def cmd_ingest(args):
    if (cfg := _load("ingest")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "ingest")) is None:
        return EXIT_REFUSED
    if (store := _open_store(cfg.store_path, "ingest")) is None:
        return EXIT_REFUSED
    logger.info("ingesting %d path(s): %s", len(args.paths), ", ".join(str(Path(p).resolve()) for p in args.paths))
    return _report(store.ingest_paths(args.paths))


def cmd_series(args):
    if (cfg := _load("series")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "series")) is None:
        return EXIT_REFUSED
    if (store := _open_store(cfg.store_path, "series")) is None:
        return EXIT_REFUSED
    listed = store.series(args.workload, args.quantity)
    if not listed:
        logger.info("no series%s", " matches the filter" if args.workload or args.quantity else " in the store")
    for s in listed:
        estimator = json.loads(s["estimator"])["method_version"]
        print(
            f"{s['fingerprint'][:12]}  {s['project']}  {s['workload']}  {s['quantity']} [{s['unit']}]  est={estimator}"
        )
    return EXIT_OK


def cmd_history(args):
    if (cfg := _load("history")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "history")) is None:
        return EXIT_REFUSED
    if (store := _open_store(cfg.store_path, "history")) is None:
        return EXIT_REFUSED
    points = store.history(args.series)
    if not points:
        logger.info("no series matches %s", args.series)
    elif len(matched := {p["series_fingerprint"] for p in points}) > 1:
        logger.warning(
            "%s matches %d series, whose points are listed together; `changed` is worked out per series: %s",
            args.series,
            len(matched),
            ", ".join(sorted(f[:12] for f in matched)),
        )
    print("revision order unavailable without a revision graph; listed by measurement start")
    for p in points:
        changed = f"  changed: {', '.join(p['changed'])}" if p["changed"] else ""
        warned = f"  [{', '.join(p['warnings'])}]" if p["warnings"] else ""  # such as dirty-subject-tree
        print(
            f"{p['revision'][:12]}  {p['started_at']}  {p['value']:.6g} ({p['source']})  {p['ingest_key']}"
            f"{changed}{warned}"
        )
    return EXIT_OK


def cmd_head(args):
    if (cfg := _load("head")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "head")) is None:
        return EXIT_REFUSED
    if (store := _open_store(cfg.store_path, "head")) is None:
        return EXIT_REFUSED
    total, rows = store.head(args.n, args.project, args.workload, measurands=args.table)
    if args.json:
        for row in rows:
            print(row["document"])
        return EXIT_OK
    if not total:
        logger.info(
            "%s holds no result%s", store.path, "s that match the filter" if args.project or args.workload else ""
        )
    print(f"{store.path}: {total} result(s); latest {len(rows)}, newest first")
    if args.table:  # the columns of `bx bench`, led by where each result came from; measurands only
        print(
            bench_mod.render(
                [
                    {
                        **bench_mod.row_of(json.loads(r["document"])),
                        "revision": r["revision"][:10],
                        "run": r["run_key"],
                        "project": r["project"],
                    }
                    for r in rows
                ],
                leading=("revision", "run", "project"),
            )
        )
        return EXIT_OK
    cells = []
    for r in rows:
        median = f"{r['median']:.6g} {r['unit']}" if r["median"] is not None else "-"
        started = r["started_at"].strftime("%Y-%m-%d %H:%M:%S")
        labels = f"  {r['labels']}" if r["labels"] else ""
        codes = warning_codes(json.loads(r["document"]))  # such as dirty-subject-tree, as `--table` shows them
        status = r["status"] + (f"  [{', '.join(codes)}]" if codes else "")
        cells.append(
            (
                f"{r['row']:>5}",
                started,
                r["run_key"],
                r["workload"],
                r["quantity"],
                status,
                median,
                r["revision"][:10],
                f"{r['project']}{labels}",
            )
        )
    widths = [max([len(cell[i]) for cell in cells], default=0) for i in range(len(cells[0]))] if cells else []
    for cell in cells:
        print("  ".join(text.ljust(width) for text, width in zip(cell, widths)).rstrip())
    return EXIT_OK


def cmd_target(args):
    """Ask for a target's description: `describe` only reports, `prepare` may
    build (the provider's own tools do that, never benchx)."""
    if (cfg := _load("target")) is None:
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "target")) is None:
        return EXIT_REFUSED
    try:
        _needs_provider(cfg, args.target)
        description = target_mod.resolve(args.target, operation=args.operation, provider=cfg.provider)
    except (target_mod.TargetError, config_mod.ConfigError) as e:
        say.error(f"cannot {args.operation} {args.target}: {e}")
        return EXIT_REFUSED
    if description is None:
        say.warning(f"{args.target} has no description: no {target_mod.SIDECAR} in it")
        return EXIT_INVALID
    print(json.dumps(description, indent=1))
    return EXIT_OK


class UsageError(Exception):
    """The arguments do not select a mode, or mix the two."""


# Flags that belong to one mode of `bx compare` (prototype-design.md §1).
_MEASURE_ONLY = (
    "adapter",
    "suite",
    "filter",
    "quantity",
    "rounds",
    "env",
    *harnesses.FLAG_NAMES,
    "project",
    "no_project",
    "run_key",
    "out",
    "source_uri",
    "no_build",
    "no_ingest",
)
_READ_ONLY = ("baseline", "results")


def _flags(args, names):
    return [f"--{n.replace('_', '-')}" for n in names if getattr(args, n) not in (None, [], False)]


def _mode(args, profile) -> str:
    """read or measure. Measure mode takes two targets, except for variants, whose sides are two
    values of one parameter of one target; and there `--baseline` picks the baseline value."""
    variants = profile == "variants"
    targets = "one target" if variants else "two targets"
    if args.targets and args.run:
        raise UsageError(f"give {targets} or --run, not both")
    if not args.targets and not args.run:
        raise UsageError(f"give {targets} (measure mode) or --run KEY (read mode)")
    if args.targets:
        if len(args.targets) != (1 if variants else 2):
            raise UsageError(
                "measure mode takes exactly one target for variants: TARGET"
                if variants
                else "measure mode takes exactly two targets: BASELINE CONTENDER"
            )
        if wrong := _flags(args, ("results",) if variants else _READ_ONLY):
            raise UsageError(f"{', '.join(wrong)} belongs to read mode (--run)")
        return "measure"
    if wrong := _flags(args, _MEASURE_ONLY):
        raise UsageError(f"{', '.join(wrong)} belongs to measure mode ({targets})")
    if not args.baseline:
        raise UsageError("read mode needs --baseline")
    return "read"


def cmd_compare(args):
    if (cfg := _load("compare")) is None:
        return EXIT_REFUSED
    try:
        policy, sources = _policy(cfg, args)
        mode = _mode(args, policy["profile"])
        _profile_flags(args, policy["profile"])
        _view_flags(args)
    except (UsageError, config_mod.ConfigError) as e:
        say.error(f"bx compare: error: {e}")
        return EXIT_REFUSED
    sources.append(("mode", mode, "inferred: " + ("targets were given" if mode == "measure" else "--run was given")))
    return _measure(cfg, args, policy, sources) if mode == "measure" else _read(cfg, args, policy, sources)


def _policy(cfg, args) -> tuple[dict, list]:
    """The comparison's own settings: the command line, else `compare` in the file, else the default;
    and each with where it came from, for the log."""
    compare = cfg.section("compare")
    profile, profile_source = cfg.pick_with_source(
        args.profile, compare.get("profile"), flag="--profile", key="compare.profile"
    )
    k, k_source = cfg.pick_with_source(args.k, compare.get("k"), 3.0, flag="--k", key="compare.k")
    min_rounds, min_source = cfg.pick_with_source(
        args.min_rounds, compare.get("min_rounds"), 4, flag="--min-rounds", key="compare.min_rounds"
    )
    return (
        {"profile": profile, "k": k, "min_rounds": min_rounds},
        [
            ("compare.profile", profile, profile_source),
            ("compare.k", k, k_source),
            ("compare.min_rounds", min_rounds, min_source),
        ],
    )


def _profile_flags(args, profile):
    """The flags that name the sides of one profile only."""
    if args.vary and profile != "variants":
        raise UsageError(
            f"--vary names the parameter whose values are the sides of a variants comparison, not of {profile}"
        )
    if args.label and profile == "variants":
        raise UsageError(
            "--label names the sides of an environments comparison; the sides of a variants "
            "comparison are two values of a parameter (--vary)"
        )


def _view_flags(args):
    """--tables is a text view of the comparison, and --all a switch of it."""
    if args.tables and args.json:
        raise UsageError("--tables is a text view: not with --json")
    if args.all and not args.tables:
        raise UsageError("--all goes with --tables")


def _read(cfg, args, policy, sources):
    # A read stores nothing, so its records join the run's own log when the run folder exists;
    # and a log that cannot be opened there is skipped, not refused, unless a flag named it.
    run_dir = session.run_directory(cfg.results_dir, args.run)
    if (cfg := _start_logging(cfg, args, run_dir if run_dir.is_dir() else None, "compare", lenient=True)) is None:
        return EXIT_REFUSED
    _log_settings(sources)
    if args.results:
        store = MemoryStore()  # the ad hoc path: validated in memory without PyArrow
        outcome = store.ingest_paths(args.results)
        # The throwaway store is discarded on exit: its path would say nothing, so the count does.
        logger.debug("read mode with --results: %d document(s) ingested into a throwaway store", outcome["ingested"])
        if outcome["rejected"]:
            logger.warning("%d document(s) were rejected: the comparison was not run", len(outcome["rejected"]))
            return _report(outcome)
        return _compare(store, args, policy)
    store = _open_store(cfg.store_path, "compare", hint="pass --results DIR to compare JSON results directly")
    if store is None:
        return EXIT_REFUSED
    return _compare(store, args, policy)


def _measure(cfg, args, policy, sources):
    """The workbench's session loop: run both sides, deliver, compare."""
    try:  # the check phase: nothing has run, and no folder exists yet
        settings = _common(cfg, args)
        session.check_harness(
            settings["adapter"], settings["flags"], settings["file_precision"], settings["quantities"]
        )
        _needs_provider(cfg, *args.targets)
        rounds, rounds_source = cfg.pick_with_source(
            args.rounds, cfg.section("compare").get("rounds"), session.ROUNDS, flag="--rounds", key="compare.rounds"
        )
        label = session.parse_label(args.label) if args.label else None
    except (UsageError, config_mod.ConfigError) as e:
        say.error(f"bx compare: error: {e}")
        return EXIT_REFUSED
    except session.SessionError as e:
        say.error(f"cannot compare: {e}")
        return EXIT_REFUSED
    if args.no_ingest:
        store = MemoryStore()
    else:
        store = _open_store(
            cfg.store_path,
            "compare",
            hint="pass --no-ingest to compare without persisting to the local store",
        )
        if store is None:
            return EXIT_REFUSED
    run_key = args.run_key or orders.default_run_key("compare")
    out = Path(args.out) if args.out else session.run_directory(cfg.results_dir, run_key)
    if (cfg := _start_logging(cfg, args, out, "compare")) is None:
        return EXIT_REFUSED
    if not args.no_ingest:
        say.info(
            f"{store.path}"
        )  # stderr, so --json output stays clean; after the checks, so a refusal does not print it
    _log_settings([*sources, *settings.pop("sources"), ("compare.rounds", rounds, rounds_source)])
    settings["run_key"], settings["out"] = run_key, out
    try:
        if policy["profile"] == "variants":
            doc = session.compare_variants(
                *args.targets,
                rounds=rounds,
                store=store,
                parameter=args.vary,
                baseline=args.baseline,
                progress=say.info,
                k=policy["k"],
                min_rounds=policy["min_rounds"],
                **settings,
            )
        else:
            doc = session.compare_targets(
                *args.targets, rounds=rounds, store=store, label=label, progress=say.info, **policy, **settings
            )
    except runner.Refused as e:
        return _refused(e)
    except (UsageError, config_mod.ConfigError) as e:
        say.error(f"bx compare: error: {e}")
        return EXIT_REFUSED
    except (session.SessionError, compare_mod.CompareError) as e:
        say.error(f"cannot compare: {e}")
        return EXIT_REFUSED
    _show(doc, args, store.documents(doc["selector"]["params"]["run_key"]) if args.tables else None)
    return EXIT_OK


def _degraded(doc):
    """What a comparison that exited 0 left undone, for the footer: nothing is added for a clean one."""
    units, failed = doc["units"], doc.get("failed_invariants", [])
    if failed and not units:
        log.degraded(f"0 units compared, {len(failed)} invariant(s) failed")
    elif failed:
        log.degraded(f"{len(failed)} invariant(s) failed")
    if indeterminate := [u for u in units if u["verdict"] == "indeterminate"]:
        log.degraded(f"{len(indeterminate)} of {len(units)} units indeterminate")


def _show(doc, args, documents):
    """The comparison as JSON, or as text; with --tables, the asv-style tables follow the summary."""
    _degraded(doc)
    if args.json:
        print(json.dumps(doc, indent=1))
        return
    print(compare_mod.render(doc))
    if args.tables:
        shown = tables_mod.render(doc, documents, all_rows=args.all)
        if not shown:
            logger.warning(
                "tables skipped: no units, or no contender (%d failed invariant(s))",
                len(doc.get("failed_invariants", [])),
            )
        print(shown)


def _compare(store, args, policy):
    try:
        documents = store.documents(args.run)
        parameter = None
        if policy["profile"] == "variants":
            parameter = args.vary or compare_mod.infer_parameter(documents, args.run)
            if not args.vary:  # the choice is recorded in the document's `varying`, and said here too
                say.info(f"varying: {parameter} (the only parameter)")
                logger.info("%s was inferred from %d result document(s) of run %s", parameter, len(documents), args.run)
        doc = compare_mod.compare(
            documents,
            run_key=args.run,
            profile=policy["profile"],
            baseline=args.baseline,
            label=args.label,
            parameter=parameter,
            k=policy["k"],
            min_rounds=policy["min_rounds"],
        )
    except compare_mod.CompareError as e:
        say.error(f"cannot compare: {e}")
        return EXIT_REFUSED
    _show(doc, args, documents)
    return EXIT_OK


def cmd_validate(args):
    # `validate` does not read the config file: the logging settings come from the flags and the environment.
    if _start_logging(_no_config(), args, None, "validate", read_config=False) is None:
        return EXIT_REFUSED
    valid = 0
    logger.info("validating %d file(s) as %s", len(args.files), args.kind)
    for path in args.files:
        inspection = core.inspect(path, args.kind)
        for error in inspection.errors:
            say.warning(f"{path}: {error}")
        if inspection.valid:
            logger.debug("%s is a valid %s", path, args.kind)
        valid += inspection.valid
    say.info(f"{valid}/{len(args.files)} valid")
    return EXIT_OK if valid == len(args.files) else EXIT_INVALID


def cmd_view(args):
    """Build static dashboard views and optionally serve them over local HTTP."""
    if (cfg := _load("view")) is None:
        return EXIT_REFUSED
    serve = args.serve or args.out is None
    if args.open and not serve:
        say.error("bx view: error: --open goes with serving (omit --out, or pass --serve)")
        return EXIT_REFUSED
    if (cfg := _start_logging(cfg, args, None, "view")) is None:
        return EXIT_REFUSED

    source_kwargs: dict = {}
    if args.runs is not None:
        source_kwargs["runs_dir"] = args.runs
    elif args.results is not None:
        source_kwargs["results_paths"] = list(args.results)
    else:
        source_kwargs["store_path"] = args.store if args.store is not None else cfg.store_path

    if args.marts:
        write_marts: bool | None = True
    elif args.no_marts:
        write_marts = False
    elif args.out is None:
        write_marts = False
    else:
        write_marts = None

    repo = Path(args.repo) if args.repo is not None else cfg.root
    build_kwargs = {
        **source_kwargs,
        "project_file": Path(args.project_file) if args.project_file else None,
        "title": args.title,
        "repo": repo,
        "write_marts": write_marts,
        "views_format": args.views_format,
        "stamp": args.stamp,
        "config": cfg,
    }

    def _run_build(out_dir: Path) -> dict | None:
        try:
            return views_mod.build_site(out_dir, **build_kwargs)
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] == "pyarrow":
                hint = ""
                if "store_path" in source_kwargs:
                    hint = ", or pass --results DIR to build views from JSON results directly"
                say.error(f"bx view: error: {exc}{hint}")
                return None
            raise
        except views_mod.ViewError as exc:
            say.error(f"bx view: error: {exc}")
            return None

    port = 8000 if args.port is None else args.port
    fallback_port = args.port is None

    def _finish(summary: dict, out_dir: Path) -> int:
        exit_code = EXIT_OK
        if summary["outcome"] and summary["outcome"]["rejected"]:
            exit_code = _report(summary["outcome"])
        counts = summary["counts"]
        if args.out is not None:
            print(
                f"{counts['points']} result(s), {counts['series']} series, "
                f"{counts['distinct_commits']} commit(s) -> {summary['out']}"
            )
        else:
            print(f"{counts['points']} result(s), {counts['series']} series, {counts['distinct_commits']} commit(s)")
        if serve:
            try:
                views_mod.serve_site(
                    out_dir,
                    host=args.host,
                    port=port,
                    fallback_port=fallback_port,
                    open_browser=args.open,
                )
            except OSError as exc:
                say.error(f"bx view: error: cannot serve on {args.host}:{port}: {exc}")
                return EXIT_REFUSED
        return exit_code

    if args.out is not None:
        out_dir = Path(args.out)
        summary = _run_build(out_dir)
        if summary is None:
            return EXIT_REFUSED
        return _finish(summary, out_dir)

    with tempfile.TemporaryDirectory(prefix="benchx-view-") as tmp:
        out_dir = Path(tmp)
        summary = _run_build(out_dir)
        if summary is None:
            return EXIT_REFUSED
        return _finish(summary, out_dir)


def _add_target_options(parser, command):
    """The options `bx bench` and `bx compare` (measure mode) share: the harness,
    its suite and cases, its precision flags, and where the results go."""
    parser.add_argument(
        "--adapter", choices=sorted(harnesses.HARNESSES), help="the harness (default: adapter.name; required)"
    )
    parser.add_argument(
        "--suite",
        help="pyperf: the module's path in the working tree; "
        "google-benchmark: the binary in the build directory "
        "(default: adapter.suite; required)",
    )
    parser.add_argument(
        "--filter",
        help="a case filter regex (pyperf, asv: searched in the name, and in "
        "name(p0, p1) of a parametrized benchmark; "
        "google-benchmark: --benchmark_filter)",
    )
    parser.add_argument(
        "--quantity",
        metavar="NAMES",
        help="comma-separated, collected in one run (default: adapter.quantities, else wall-time); "
        "google-benchmark also reports cpu-time",
    )
    parser.add_argument(
        "-e",
        "--env",
        action="append",
        metavar="NAME=VALUE",
        help="set an environment variable for the benchmark (repeatable); each becomes a workload parameter",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        help=f"google-benchmark: repetitions per case (default {harnesses.GOOGLE_BENCHMARK.DEFAULT_REPETITIONS})",
    )
    parser.add_argument("--processes", type=int, help="pyperf: worker processes (default 20)")
    parser.add_argument("--values", type=int, help="pyperf: values per process (default 3)")
    parser.add_argument("--min-time", type=float, help="adaptive calibration: minimum seconds per sample")
    parser.add_argument("--loops", type=int, help="pyperf: fixed calibration, inner loops per value")
    parser.add_argument("--warmups", type=int, help="pyperf: warmup values per process (0: none)")
    parser.add_argument("--project", help="name a project, so the results join its series (default: project)")
    parser.add_argument(
        "--no-project",
        action="store_true",
        help="an ad hoc run, whatever the file's project says: filed as local/<hostname>",
    )
    parser.add_argument("--run-key", help=f"default: {command}-<UTC timestamp>")
    parser.add_argument(
        "--out",
        help="directory for orders and result files (default: RESULTS/RUN_KEY, "
        "RESULTS being paths.results, else ./results in the project root)",
    )
    parser.add_argument(
        "--harness-verbosity",
        type=_non_negative,
        metavar="N",
        help="ask the harness to say more (default 0: nothing more). pyperf and asv: 1 or more passes "
        "--verbose; Google Benchmark: N is passed as --v=N. Its output is logged in full. "
        f"Each -v past -vv does the same (-vvv is 1). ${HARNESS_VERBOSITY_ENV}, adapter.verbosity",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="ask a provider to describe each @CONFIG target instead of preparing (building) it",
    )
    parser.add_argument(
        "--source-uri", help="the source's URI (default: source.uri, else the checkout's origin remote)"
    )


def _logging_options() -> argparse.ArgumentParser:
    """The options every subcommand takes: where the log goes, how much it keeps, and how much
    the terminal shows. The three switch flags share one destination, so
    `--no-log`, `--log` and `--log-file PATH` exclude each other. None means not given, so
    any flag beats the environment and the file."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_mutually_exclusive_group()
    group.add_argument(
        "--no-log",
        dest="log",
        action="store_const",
        const=False,
        default=None,
        help="write no log (the default is bx.log in the run's output folder)",
    )
    group.add_argument(
        "--log",
        dest="log",
        action="store_const",
        const=True,
        default=None,
        help="send the whole log to stderr instead of a file",
    )
    group.add_argument(
        "--log-file",
        dest="log",
        metavar="PATH",
        help="append the log to PATH ({out} is the run's output folder, {root} the project root)",
    )
    parent.add_argument(
        "--log-level",
        type=str.lower,
        choices=list(log.LEVELS),
        metavar="LEVEL",
        help=f"what the log keeps: {', '.join(log.LEVELS)} (default debug; ${LOG_LEVEL_ENV})",
    )
    parent.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=None,
        help=f"show more on the terminal: -v every INFO record, -vv DEBUG too; each -v past -vv also asks the "
        f"harness to say more (--harness-verbosity) (${VERBOSE_ENV})",
    )
    parent.add_argument(
        "-q",
        "--quiet",
        action="count",
        default=None,
        help="show less on the terminal: -q hides the table and the other INFO messages (it keeps warnings, "
        "errors, the start line, the not-measured section and the result line); -qq keeps "
        "only errors and the not-measured section",
    )
    return parent


def main(argv=None):
    options = _logging_options()
    ap = argparse.ArgumentParser(prog="bx", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", parents=[options], help="execute one work order (schemas/work-order/0.1.0)")
    p.add_argument("order")
    p.add_argument("--out", help="directory for result files (default ./results)")
    p.add_argument(
        "--no-ingest", action="store_true", help="only write the result files; do not deliver them to the local store"
    )
    p.add_argument(
        "--allow-collectors",
        action="store_true",
        help="run the order's context collectors: commands the order names (otherwise refused)",
    )
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser(
        "bench",
        parents=[options],
        help="benchmark one target: the single-target form of bx compare",
        description="bx bench TARGET --suite S [FUNCTION ...]: run one target once and print a table "
        "per case. TARGET and the options are those of bx compare: @CONFIG[=SOURCE_REF] "
        "(the project's target provider) or a path, which the harness reads (pyperf: a "
        "working tree; google-benchmark: BUILD_DIR[:SOURCE_DIR]). Each FUNCTION is an "
        "exact case name; with none, the harness's default cases. The shorthand "
        "bx bench MODULE.py [FUNCTION ...] benchmarks a Python module with pyperf: "
        "its checkout is the target, and its path there the suite. The shorthand "
        "bx bench PATH --adapter asv runs an asv suite, timed by pyperf: PATH holds "
        "asv.conf.json (its benchmark_dir is the suite) or is the benchmark directory; "
        "--filter searches name(p0, p1) per parameter combination, as asv run --bench, "
        "and the precision flags left out come from each benchmark's own attributes. "
        "Precision comes "
        "from the harness's defaults, the config file (pyperf; bx config init "
        "writes one) and the flags: flags win.",
    )
    p.add_argument(
        "target",
        metavar="TARGET",
        help="@CONFIG[=SOURCE_REF], a path, or (with no --suite) a Python file or an asv suite",
    )
    p.add_argument(
        "functions",
        nargs="*",
        metavar="FUNCTION",
        help="exact case names (default: all cases); pyperf, asv: the name of a "
        "parametrized benchmark selects all its combinations",
    )
    _add_target_options(p, "bench")
    p.add_argument("--tree", help="the shorthand's tree (default: the module's or suite's git checkout or directory)")
    p.add_argument(
        "--case-timeout",
        type=float,
        help=f"seconds before one case is stopped (default: timeouts.case_s, else {orders.CASE_TIMEOUT_S:g}; "
        f"asv: {ASV_CASE_TIMEOUT_S}, all rounds, each also stopped at the benchmark's own timeout)",
    )
    p.add_argument(
        "--label",
        action="append",
        metavar="NAME=VALUE",
        help="name this call's side of a session, such as env=numpy-2.0 (repeatable); the calls of a "
        "session share --run-key, and bx compare --run reads the label with --label NAME. "
        "Needs --run-key and --slot",
    )
    p.add_argument(
        "--round",
        metavar="R[/N]",
        help="this call's round in a session, numbered from 0: the sides' attempts are paired by it. "
        "R/N also says the session has N rounds, which the start line shows (nothing else uses "
        "it). Needs --run-key and --slot",
    )
    p.add_argument(
        "--slot",
        type=int,
        help="this call's position in the session's alternation (0, 1, 2, ...), kept by the caller's loop. "
        "Needs --run-key",
    )
    p.add_argument("--no-ingest", action="store_true", help="only write result files; skip the local store")
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("config", parents=[options], help="write or show benchx.conf.json")
    p.add_argument(
        "action",
        choices=["init", "show"],
        help="init: write a benchx.conf.json stating a harness's defaults; show: what the file and the defaults give",
    )
    p.add_argument("--dir", default=".", help="init: where to write benchx.conf.json (default .)")
    p.add_argument(
        "--adapter",
        choices=sorted(harnesses.HARNESSES),
        default="pyperf",
        help="init: the harness (default %(default)s)",
    )
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("ingest", parents=[options], help="sweep result files and work orders into the local store")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("series", parents=[options], help="list series")
    p.add_argument("--workload")
    p.add_argument("--quantity")
    p.set_defaults(fn=cmd_series)

    p = sub.add_parser("history", parents=[options], help="one series' points (a query; history mode is deferred)")
    p.add_argument("series", help="series fingerprint or prefix")
    p.set_defaults(fn=cmd_history)

    p = sub.add_parser("head", parents=[options], help="the latest results in the store, newest first")
    p.add_argument("-n", type=int, default=10, help="how many (default 10)")
    p.add_argument("--project", help="only this store project's results (e.g. numpy, local/HOST)")
    p.add_argument("--workload", help="only results whose benchmark name contains this")
    shape = p.add_mutually_exclusive_group()
    shape.add_argument("--json", action="store_true", help="print the stored documents instead")
    shape.add_argument(
        "--table",
        action="store_true",
        help="the `bx bench` table (median, mean ± sd, range, n, status) with revision, run and project",
    )
    p.set_defaults(fn=cmd_head)

    p = sub.add_parser(
        "target",
        parents=[options],
        help="ask for a target's description (schemas/target-description/0.1.0)",
        description="TARGET is @CONFIG[=SOURCE_REF], asking the provider named in "
        "benchx.conf.json, or a build directory holding .benchx-target.json. "
        "describe only reports a target that exists; prepare may build it.",
    )
    p.add_argument("operation", choices=["describe", "prepare"])
    p.add_argument("target")
    p.set_defaults(fn=cmd_target)

    p = sub.add_parser(
        "compare",
        parents=[options],
        help="compare two sides: read mode (--run) or measure mode (two targets)",
        description="Read mode compares results already measured: --run KEY --baseline VALUE. "
        "Measure mode runs two prepared targets, alternating them over "
        "--rounds rounds, delivers the results to the local store, then compares: "
        "BASELINE CONTENDER --suite NAME --rounds R, each target BUILD_DIR[:SOURCE_DIR] "
        "or @CONFIG[=SOURCE_REF] (the project's target provider). "
        "The variants profile takes one target: its sides are two values of one "
        "benchmark parameter (--vary), as in TARGET --profile variants --suite NAME "
        "--rounds R.",
    )
    p.add_argument("targets", nargs="*", metavar="TARGET", help="measure mode: BASELINE CONTENDER (variants: TARGET)")
    p.add_argument("--profile", choices=list(compare_mod.PROFILES), help="default: compare.profile")
    p.add_argument(
        "--vary",
        metavar="NAME",
        help="variants: the workload parameter whose two values are the sides "
        "(default: the only parameter the results carry)",
    )
    p.add_argument(
        "--k",
        type=float,
        help="confidence in normal sigmas: the threshold is the t quantile with that two-sided tail, "
        "times the noise (default: compare.k, else 3)",
    )
    p.add_argument(
        "--min-rounds", type=int, help="fewest paired rounds for a verdict (default: compare.min_rounds, else 4)"
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--tables", action="store_true", help="after the summary, asv-style tables: one per side, then the changes"
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="with --tables: every row of the change table, not only the changed ones (as asv continuous)",
    )
    p.add_argument(
        "--label", help="read mode: the label that names the sides (environments); measure mode: NAME=BASE,CONTENDER"
    )
    p.add_argument(
        "--baseline",
        help="revisions: a revision or tree id prefix; environments: the label value; "
        "variants: the parameter's recorded value, quotes included ('baseline'); "
        "required in read mode, and in measure mode only for variants "
        "(default: the first declared value)",
    )
    read = p.add_argument_group("read mode")
    read.add_argument("--run", help="run key")
    read.add_argument(
        "--results", nargs="+", help="compare these result files or directories directly, without using the local store"
    )
    measure = p.add_argument_group("measure mode")
    measure.add_argument(
        "--rounds",
        type=int,
        help=f"alternation rounds; each runs both sides once (default: compare.rounds, else {session.ROUNDS})",
    )
    measure.add_argument(
        "--no-ingest", action="store_true", help="write result files and compare in memory; skip the local store"
    )
    _add_target_options(measure, "compare")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser(
        "validate",
        parents=[options],
        help="check documents against their schema and rules",
        description="Report every problem in each document, or nothing when all are valid.",
    )
    p.add_argument("files", type=Path, nargs="+")
    p.add_argument(
        "--kind",
        choices=get_args(Kind),
        default="measurement-result",
        help="kind of document being checked (default: %(default)s)",
    )
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser(
        "view",
        parents=[options],
        help="build and serve the static benchmark dashboard",
        description="Build static dashboard views from the local Parquet store, JSON result files "
        "(--results, no PyArrow required), or a runs/<sha>/<node>/ directory (--runs). "
        "With no --out, builds into a temporary directory and serves locally until Ctrl-C; "
        "with --out DIR, writes the static site (add --serve to also serve it).",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--store", help="local Parquet store (default: paths.store / $BENCHX_HOME/store.parquet)")
    src.add_argument("--results", nargs="+", help="JSON result files or directories, read directly without PyArrow")
    src.add_argument("--runs", help="runs/<sha>/<node>/ directory holding run.parquet and results.parquet")
    p.add_argument("--out", help="write the static site into DIR instead of a temporary directory")
    p.add_argument("--serve", action="store_true", help="serve the built site over local HTTP")
    p.add_argument("--host", default="127.0.0.1", help="bind address for local serving (default %(default)s)")
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port for local serving (default 8000, falling back to a free port if busy)",
    )
    p.add_argument("--open", action="store_true", help="open the served dashboard in the default web browser")
    p.add_argument("--project-file", help="project.json describing title, links, and upstream commit URL")
    p.add_argument("--title", help="override the dashboard page title")
    p.add_argument("--repo", help="git checkout used to enrich commit metadata for --store / --results")
    marts_group = p.add_mutually_exclusive_group()
    marts_group.add_argument(
        "--marts", action="store_true", help="write derived Parquet tables under data/ (requires benchx[parquet])"
    )
    marts_group.add_argument("--no-marts", action="store_true", help="skip writing derived Parquet tables under data/")
    p.add_argument(
        "--views-format",
        choices=("json", "parquet"),
        default="json",
        help="format for frontend view payloads under data/views/ (default %(default)s; parquet requires benchx[parquet])",
    )
    p.add_argument("--stamp", help="cache-busting version stamped onto static asset URLs in index.html")
    p.set_defaults(fn=cmd_view)

    args, extra = ap.parse_known_args(argv)
    # argparse takes a nargs="*" positional only from the first run of positionals, so cases
    # given after an option (`bx bench T --suite S CASE`) arrive here, on some Python versions.
    if args.cmd == "bench" and not any(item.startswith("-") for item in extra):
        args.functions += extra
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    args.argv = list(sys.argv[1:] if argv is None else argv)  # the header records the command as given
    try:
        args.verbosity, args.verbosity_source, args.verbose_extra = _verbosity(args)
    except UsageError as e:
        log.start_console(0)
        say.error(f"bx {args.cmd}: error: {e}")
        log.stop()
        return EXIT_REFUSED
    log.start_console(args.verbosity)  # first, so that a broken config file is still reported
    outcome, code = "crashed", None
    try:
        code = args.fn(args)
        outcome = OUTCOMES.get(code, "crashed")
        return code
    except core.catalog.FormatCheckerError as e:
        say.error(f"bx {args.cmd}: error: {e}")
        code = EXIT_REFUSED
        outcome = OUTCOMES[code]
        return code
    except SystemExit as e:  # argparse, or a command that exits: let it through
        code = e.code if isinstance(e.code, int) else 0 if e.code is None else 1
        outcome = OUTCOMES.get(code, "crashed")
        raise
    except KeyboardInterrupt:
        logger.warning("interrupted")
        outcome = "interrupted"
        raise  # Python prints its traceback and exits by the signal, as it does without a log
    except Exception:  # what Python's own handler would print, and its exit status
        say.error(traceback.format_exc().rstrip("\n"))
        code = 1
        return code
    finally:
        log.footer(outcome, code)
        log.stop()


if __name__ == "__main__":
    sys.exit(main())
