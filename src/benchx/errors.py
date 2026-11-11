"""Errors shared by the workbench commands (bx bench, bx compare)."""


class SessionError(Exception):
    """The session cannot start or finish; the message says why."""
