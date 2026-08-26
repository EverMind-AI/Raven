"""Import litellm with its terminal noise silenced.

litellm prints a "Provider List" banner (gated by ``suppress_debug_info``) and,
because it installs its own stderr ``StreamHandler`` on its ``LiteLLM*`` loggers,
emits DEBUG to the terminal *while importing*. Raise those loggers' levels across
the import so that DEBUG never reaches the terminal, then restore them.

The handlers are then detached for the rest of the session here, rather than left
to raven's ``_strip_tty_stream_handlers``, because this import is deferred: that
helper runs when logging is configured, which is before litellm exists, so on its
own it leaves the handler in place. The TUI covers the gap by stripping a second
time after the import; every other entry point does not, and under ``raven acp``
the handler writes straight into the pipe the client reads, where a reader that
stops draining blocks this process at its next log line.
"""

import logging
import sys

# litellm attaches its stderr handler to all three (litellm/_logging.py).
_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def _detach_tty_handlers(loggers: list[logging.Logger]) -> None:
    """Remove the terminal ``StreamHandler``s litellm put on its own loggers."""
    tty_streams = (sys.stderr, sys.stdout)
    for lg in loggers:
        for handler in list(lg.handlers):
            if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in tty_streams:
                lg.removeHandler(handler)


def import_litellm():
    """Import litellm with its banner disabled and its terminal handler detached."""
    loggers = [logging.getLogger(name) for name in _LITELLM_LOGGERS]
    prev_levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        import litellm

        litellm.suppress_debug_info = True
    finally:
        for lg, prev in zip(loggers, prev_levels):
            lg.setLevel(prev)

    _detach_tty_handlers(loggers)

    return litellm
