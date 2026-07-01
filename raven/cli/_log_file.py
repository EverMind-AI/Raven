"""Shared loguru→file redirection for long-lived / screen-owning CLI commands.

Both ``gateway`` (foreground long-running) and ``tui`` (Ink owns the terminal)
need loguru routed to a rotating file instead of stderr. They differ only in
filename, whether a live stderr sink is kept, and retention — all parameters.

The log directory follows :func:`get_logs_dir`, so a ``--config`` instance
writes its logs next to its own config rather than always to ``~/.raven``.

Env vars:
    RAVEN_CLI_DEBUG=1  — additionally mirror DEBUG+ to stderr.
"""

from __future__ import annotations

import logging as _stdlib_logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from raven.config.paths import get_logs_dir


def redirect_loguru_to_file(
    filename: str,
    *,
    file_level: str = "DEBUG",
    rotation: str = "10 MB",
    retention: int | str = 7,
    terminal_level: str | None = None,
    record_filter: Callable[[dict], bool] | None = None,
) -> Path:
    """Route all loguru output to ``<logs>/filename`` (rotating file sink).

    ``terminal_level`` keeps a live stderr sink at that level; ``None`` drops
    the terminal sink entirely (for screen-owning callers like the Ink TUI).

    ``record_filter`` is an optional loguru sink filter (``None`` keeps every
    record), letting one caller drop sink-specific noise without affecting others.
    """
    from loguru import logger

    log_path = get_logs_dir() / filename

    logger.remove()
    logger.add(
        str(log_path),
        level=file_level,
        rotation=rotation,
        retention=retention,
        filter=record_filter,
        enqueue=True,  # thread-safe writes from channel threads + asyncio
        # diagnose=True would annotate tracebacks with local variable values,
        # writing secrets (API tokens, etc.) into a persisted, retained file.
        backtrace=False,
        diagnose=False,
    )
    if terminal_level is not None:
        logger.add(sys.stderr, level=terminal_level)
    if os.environ.get("RAVEN_CLI_DEBUG"):
        logger.add(sys.stderr, level="DEBUG")

    _intercept_stdlib_logging(logger)
    _strip_tty_stream_handlers()
    return log_path


def _intercept_stdlib_logging(logger) -> None:
    """Route stdlib ``logging`` records into loguru so ``getLogger(...)``
    callers land in the same file sink instead of leaking to stderr."""

    class _InterceptHandler(_stdlib_logging.Handler):
        def emit(self, record: _stdlib_logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            frame, depth = _stdlib_logging.currentframe(), 2
            while frame and frame.f_code.co_filename == _stdlib_logging.__file__:
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    _stdlib_logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


def _strip_tty_stream_handlers() -> None:
    """Remove TTY ``StreamHandler``s that third-party libs attach directly to
    named loggers.

    ``basicConfig(force=True)`` only resets the ROOT logger's handlers. Some
    libraries (notably ``litellm``) install their own ``StreamHandler(stderr)``
    on named loggers at import time; the root InterceptHandler never sees those
    records because the named-logger handler fires before propagation, and the
    direct-to-TTY write overlays the Ink alt-screen. Stripping
    them keeps records reaching the file sink via ``propagate=True`` → root →
    InterceptHandler.
    """
    tty_streams = (sys.stderr, sys.stdout)
    for obj in list(_stdlib_logging.Logger.manager.loggerDict.values()):
        if not isinstance(obj, _stdlib_logging.Logger):
            continue  # skip PlaceHolder entries
        for handler in list(obj.handlers):
            if isinstance(handler, _stdlib_logging.StreamHandler) and getattr(handler, "stream", None) in tty_streams:
                obj.removeHandler(handler)


__all__ = ["redirect_loguru_to_file"]
