"""User-facing errors shared across benchx.

Every operational or usage refusal subclasses `BenchxError` so the CLI can
report it uniformly (`bx <cmd>: error: <message>`) and exit with code 2.
"""


class BenchxError(Exception):
    """Base class for user-facing refusals and configuration/usage errors."""


class UsageError(BenchxError):
    """Invalid or conflicting command-line flags, arguments, or environment variables."""


class SessionError(BenchxError):
    """The session cannot start or finish; the message says why."""
