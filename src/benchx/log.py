"""benchx's diagnostics: what `bx` decided, warned about and failed at, kept in a log.

Every module logs through the standard library: `log = logging.getLogger(__name__)`, so the
logger names are `benchx.runner`, `benchx.cli`, and so on. `benchx/__init__.py` gives the
`benchx` logger a `NullHandler`: a library caller sees nothing unless it configures logging
itself. The `bx` command installs two kinds of handler (cli.main):

* the console handler (`start_console`), which writes to stderr exactly what `print` wrote
  before logging existed: the bare message, for the records that carry `console=True` (the
  messages `bx` shows its user, made with `console()`), or for every record under `-v`;
* the stdout handler, installed with it, for the records that carry `stdout=True` as well
  (`console().out()`): what a command that did work reports on stdout, a run's table and its
  result line. The log keeps them too. `-q` and `-qq` hide them by tier (`TABLE`, `SUMMARY`,
  `ANSWER`). A progress message with a tier (`console().progress()`, the line a call starts
  with) is shown on stderr at `-q` as well;
* a file, or with `--log` stderr, for the whole log (`configure`), in one line format.

At the default verbosity the terminal shows what `bx` showed before there was a log, and the log changes neither
what a command stores nor its exit status. `-q` and `-qq` hide the stdout of a run that ended (the table, and by tier
the rest of its report); they are the only options that take anything off it. Query answers and machine-readable
output (`--json`, `bx head`, `bx target`) stay `print`: they are not records.
"""

import datetime
import logging
import os
import shlex
import sys
import time
from pathlib import Path

PACKAGE = "benchx"
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
# What the terminal shows at each verbosity (-qq to -vv): its level, and whether it shows only
# the messages meant for the user (console=True) or every record.
_CONSOLE = {
    -2: (logging.ERROR, True),
    -1: (logging.WARNING, True),
    0: (logging.INFO, True),
    1: (logging.INFO, False),
    2: (logging.DEBUG, False),
}
# The log's own line: UTC to the millisecond, as provenance.started_at and ended_at are written,
# then the process id, so two `bx` processes given one file stay apart.
_FORMAT = "%(asctime)s %(process)d %(name)s %(levelname)s %(message)s"
_CONTINUATION = "    "
# How many `-q` a piece of stdout survives: the table of a run goes at the first, its summary
# lines at the second, and an answer (what the user needs to see, even when failures are all)
# never goes.
TABLE, SUMMARY, ANSWER = 0, 1, 2


class _Stderr(logging.StreamHandler):
    """A stderr handler that finds `sys.stderr` each time it writes. The tests call `cli.main`
    many times in one process under `capsys`; a stream taken once would be stale."""

    def __init__(self):
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr


class _Full(logging.Formatter):
    """`2026-11-02T22:36:12.345Z 48211 benchx.runner INFO message`. A message of several lines
    keeps the prefix on the first and indents the others, so that a reader that goes by line
    (`grep` for a pid, `sort`) loses none of it."""

    def __init__(self):
        super().__init__(_FORMAT)

    def formatTime(self, record, datefmt=None):
        moment = datetime.datetime.fromtimestamp(record.created, datetime.UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"

    def format(self, record):
        first, *rest = super().format(record).split("\n")
        return "\n".join([first, *(_CONTINUATION + line for line in rest)])


class _Stdout(logging.StreamHandler):
    """A stdout handler that finds `sys.stdout` each time it writes, as `_Stderr` does. A failed
    write (a closed pipe) is the command's failure, as it is for `print`: logging would swallow it."""

    def __init__(self):
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stdout

    def handleError(self, record):
        raise  # called from emit's `except`: re-raises what failed


class _Bare(logging.Formatter):
    """The message as `print` wrote it. A record's leading blank lines (`lead`, set by `out`) are the
    layout of the default output: a quiet terminal gets none of them."""

    def format(self, record):
        return ("\n" * getattr(record, "lead", 0) if _state.verbosity >= 0 else "") + record.getMessage()


class _Shown(logging.Filter):
    """Only what `out` made, and only what the verbosity leaves: `-q` is 1, `-qq` is 2."""

    def filter(self, record):
        return bool(getattr(record, "stdout", False)) and getattr(record, "tier", ANSWER) >= quiet()


class _Visible(logging.Filter):
    """What the verbosity shows on stderr: the records at its level, and the progress records (`progress`)
    whose tier `-q` leaves. The handler itself takes everything from INFO, so that these get here."""

    def filter(self, record):
        if record.levelno >= _CONSOLE[_state.verbosity][0]:
            return True
        return getattr(record, "tier", None) is not None and record.tier >= quiet()


class _NotStdout(logging.Filter):
    """stdout carries those records already: stderr does not repeat them, `-v` included."""

    def filter(self, record):
        return not getattr(record, "stdout", False)


class _OnlyConsole(logging.Filter):
    def filter(self, record):
        return bool(getattr(record, "console", False))


class _Buffer(logging.Handler):
    """Keeps the records made before the log is on (the config loaded, the shorthand's tree found, an
    `asv.conf.json` read in the command's own checks) until `configure` knows where they go. Bounded."""

    LIMIT = 1000

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        if len(self.records) < self.LIMIT:
            self.records.append(record)


class _State:
    verbosity = 0
    buffer = None  # records made before configure()
    console = None  # the console handler, or None
    stdout = None  # the stdout handler, or None
    destination = None  # the file handler, or the --log stderr handler, or None
    level = logging.DEBUG  # the level the destination keeps
    configured = False  # a destination is on, so a header was written and a footer is due
    started = None  # time.monotonic() at the header
    degraded: list = []
    once: set = set()


_state = _State()


def _logger() -> logging.Logger:
    return logging.getLogger(PACKAGE)


def _apply_level() -> None:
    """The `benchx` logger passes the lowest level any handler wants, so a record nobody reads
    costs nothing (and `isEnabledFor(DEBUG)` is false when nothing wants DEBUG)."""
    wanted = [logging.DEBUG] if _state.buffer is not None else []
    if _state.console is not None:
        wanted.append(_state.console.level)
    if _state.stdout is not None:
        wanted.append(logging.INFO)  # what `out` says goes by its tier, not its level
    if _state.destination is not None:
        wanted.append(_state.level)
    # With nothing to serve the logger defers to the root's, which is WARNING unless a library caller has set
    # it: a fixed WARNING here would outlive `stop()` and hide the records from a caller that logs at INFO.
    _logger().setLevel(min(wanted) if wanted else logging.NOTSET)


def _remove(handler) -> None:
    if handler is not None:
        _logger().removeHandler(handler)
        handler.close()


class _Console(logging.LoggerAdapter):
    def progress(self, text: str, tier: int = SUMMARY) -> None:
        """A progress message on stderr that `-q` keeps (not `-qq`): where a quiet run says what it is
        doing. An ordinary `info` is gone at `-q`. In the log it is an INFO record, as `info` makes."""
        self.logger.info(text, extra={"console": True, "tier": tier})

    def out(self, text: str, tier: int = ANSWER, *, level: int = logging.INFO) -> None:
        """`text` on stdout, and in the log. `tier` is how many `-q` it survives (`TABLE`, `SUMMARY`,
        `ANSWER`). One record a line, as `harness_output` logs a process's lines: stdout gets the same
        text, and the log keeps every line stamped, a table's columns lined up, and `grep` whole.
        Leading blank lines are layout: kept on the default terminal, dropped when quiet and in the
        log. The text is not a format string: a `%` in it is left as it is."""
        body = text.lstrip("\n")
        for number, line in enumerate(body.split("\n")):
            self.logger.log(
                level,
                line,
                extra={
                    "console": True,
                    "stdout": True,
                    "tier": tier,
                    "lead": len(text) - len(body) if number == 0 else 0,
                },
            )


def console(name: str) -> _Console:
    """The logger of a module that shows the user something: its records carry `console=True`,
    so the console handler writes them at the default verbosity. Used by `cli.py`, and by the
    prints of `adapters/asv.py` that logging replaces. Its `out` is for what goes to stdout."""
    return _Console(logging.getLogger(name), {"console": True})


def quiet() -> int:
    """How many `-q` the terminal is asked for, 0 at the default and under `-v`."""
    return max(0, -_state.verbosity)


def start_console(verbosity: int = 0) -> None:
    """Install the console handler: stderr, the bare message. `verbosity` is -2 to 2 (`-qq` to
    `-vv`). Idempotent: it replaces its own handler and leaves any other alone."""
    _remove(_state.console)
    _remove(_state.stdout)
    _remove(_state.buffer)
    _state.buffer = _Buffer()
    _logger().addHandler(_state.buffer)
    _state.verbosity = verbosity
    level, only_console = _CONSOLE[verbosity]
    handler = _Stderr()
    handler.setLevel(min(level, logging.INFO))  # `_Visible` holds back what the level does not show
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_NotStdout())
    handler.addFilter(_Visible())
    if only_console:
        handler.addFilter(_OnlyConsole())
    _logger().addHandler(handler)
    _state.console = handler
    out = _Stdout()
    out.setLevel(logging.INFO)
    out.setFormatter(_Bare())
    out.addFilter(_Shown())
    _logger().addHandler(out)
    _state.stdout = out
    _apply_level()


def configure(setting, *, level: int = logging.DEBUG, argv=None) -> None:
    """Turn the log on or off: `setting` is False (the console handler only), True (stderr
    carries the whole log, in the full format) or a resolved `Path` (appended to, never
    truncated). Writes the header record. Idempotent: it replaces what an earlier call set up
    and closes the file it had open. OSError when a file cannot be opened."""
    _remove(_state.destination)
    _state.destination = None
    _state.configured = False
    _state.degraded = []
    _state.once = set()
    earlier = _state.buffer.records if _state.buffer else []
    if _state.buffer is not None:  # from here on the destination, if any, takes the records
        _remove(_state.buffer)
        _state.buffer = None
    if setting is False or setting is None:
        _apply_level()
        return
    if setting is True:
        # One stderr handler: the log stream, which `-v` and `-q` do not touch.
        _remove(_state.console)
        _state.console = None
        handler = _Stderr()
    else:
        handler = logging.FileHandler(Path(setting), mode="a", encoding="utf-8", errors="backslashreplace")
    handler.setLevel(level)
    handler.setFormatter(_Full())
    _logger().addHandler(handler)
    _state.destination, _state.level, _state.configured = handler, level, True
    _state.started = time.monotonic()
    _apply_level()
    _header(argv)
    # What happened before the log was on, in order. The console handler has shown what it shows, so a
    # console message is not repeated on a stderr that is now the log itself.
    for record in earlier:
        if record.levelno >= level and not (setting is True and getattr(record, "console", False)):
            handler.handle(record)


def stop() -> None:
    """Remove every handler this module installed, and close the file. `cli.main` calls it
    on the way out, so a library call that follows in the same process is silent again."""
    _remove(_state.console)
    _remove(_state.stdout)
    _remove(_state.destination)
    _remove(_state.buffer)
    _state.console = _state.stdout = _state.destination = _state.buffer = None
    # And what a command left in the state, so that a library call after `cli.main` starts from nothing (`once`,
    # `degraded` and `quiet` are read without `configure`).
    _state.configured, _state.verbosity, _state.started = False, 0, None
    _state.degraded, _state.once = [], set()
    _apply_level()


def configured() -> bool:
    """A destination is on: a header was written, and a footer is due."""
    return _state.configured


def _header(argv) -> None:
    from . import __version__

    command = "bx " + shlex.join(argv) if argv is not None else "bx"
    logging.getLogger(f"{PACKAGE}.cli").info(
        "start: benchx %s, pid %d, in %s: %s", __version__, os.getpid(), Path.cwd(), command
    )


def footer(outcome: str, code: int | None = None) -> None:
    """The record that ends an invocation: its outcome word, exit code and elapsed time, then
    what it left undone, if anything (`degraded`). A missing footer is itself the evidence of a
    process that was killed."""
    if not _state.configured:
        return
    elapsed = time.monotonic() - _state.started
    took = f"{elapsed:.1f} s" if elapsed < 10 else f"{elapsed:.0f} s"
    where = f"{outcome} (exit {code})" if code is not None else outcome
    phrases = "".join(f", {phrase}" for phrase in _state.degraded)
    logging.getLogger(f"{PACKAGE}.cli").info("end: %s, %s%s", where, took, phrases)


def degraded(text: str, *, shown: bool = False) -> None:
    """A command that exits 0 but did less than asked says so: the phrase ends the footer, and
    is logged now at WARNING, so `--log-level warning` keeps it. `shown` also puts it on stdout
    at every verbosity, for a command whose table, where the reader would see it, is not."""
    _state.degraded.append(text)
    if shown:
        console(f"{PACKAGE}.cli").out(text, ANSWER, level=logging.WARNING)
    else:
        logging.getLogger(f"{PACKAGE}.cli").warning("%s", text)


def once(key) -> bool:
    """True the first time it is called with `key` since `configure()`, False after: for a
    record that is the same in every order of a command (the first gets INFO, the rest DEBUG)."""
    if key in _state.once:
        return False
    _state.once.add(key)
    return True


def harness_output(logger, label, stream, text, *, failed, kept_at=None, limit=40) -> None:
    """A process's output in the log, after it has exited: a header record that names the stream
    and its size, then the last `limit` lines, one record each (`label| line`). DEBUG when the
    process did well, WARNING when it `failed`. `kept_at` is where the full text is kept, if
    anywhere. An empty stream is skipped. The text is the program's own, logged as written."""
    level = logging.WARNING if failed else logging.DEBUG
    if not text or not text.strip() or not logger.isEnabledFor(level):
        return
    lines = text.splitlines()
    kept = lines[-limit:]
    where = f"all in {kept_at}" if kept_at else "the full text is not kept"
    if len(kept) < len(lines):
        header = f"{label}: {stream}, {len(lines)} lines (last {limit}; {where})"
    else:
        header = f"{label}: {stream}, {len(lines)} lines" + (f" (also in {kept_at})" if kept_at else "")
    logger.log(level, "%s", header)
    for line in kept:
        logger.log(level, "%s: %s| %s", label, stream, line)


def as_text(data) -> str:
    """What a timed-out process had printed, which `subprocess` hands back as bytes, as text: decoding for the log
    never raises."""
    return data.decode(errors="backslashreplace") if isinstance(data, bytes) else data or ""


def command(args, *, scripts=None, temporary=()) -> str:
    """A command line as the log shows it, pastable into a shell (`shlex.join`). Inline code
    after `-c` that is one of `scripts` ({name: text}) is shortened to `<NAME: N lines>`; paths
    in `temporary` are marked as gone once the call returns. No environment is added: the inherited one is not logged."""
    shown = [str(a) for a in args]
    for i in range(1, len(shown)):
        if shown[i - 1] == "-c":
            for name, script in (scripts or {}).items():
                if shown[i] == script:
                    shown[i] = f"<{name}: {len(script.strip().splitlines())} lines>"
    text = shlex.join(shown)
    gone = [str(p) for p in temporary if any(str(p) in a for a in shown)]
    if gone:
        text += f"  (temporary, removed after the call: {', '.join(gone)})"
    return text
