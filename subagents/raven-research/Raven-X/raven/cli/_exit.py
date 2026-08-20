"""Exit path for a process that hosts native runtimes.

Raven starts native runtimes on daemon threads (a skill-tree watcher on almost
every command, a lancedb event loop when that backend opens), and finalizing
CPython while one is live can segfault (SIGSEGV, exit 139) -- masking the
command's real exit code. Which runtimes those are, and which can be shut down,
is :mod:`raven.utils.native_runtimes`' business.

The order here is the whole point: stop what can be stopped, and only skip
finalization for what cannot. Hard-exiting unconditionally would be simpler and
strictly worse -- ``os._exit`` runs no ``atexit`` hook and flushes nothing this
function did not flush by hand, and this path now covers essentially every CLI
invocation rather than only lancedb-backed ones.

CliRunner invokes the Typer ``app`` object directly (in-process), never the
console-script ``run`` wrapper, so test hosts keep normal exit semantics.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from raven.utils.native_runtimes import native_finalization_hazard, shutdown_native_runtimes


def settle_native_runtimes() -> bool:
    """Shut down every stoppable native runtime; report whether a hazard remains.

    ``True`` means something unstoppable is still live and the caller must not
    let the interpreter finalize -- see :func:`flush_and_hard_exit`.
    """
    shutdown_native_runtimes()
    return native_finalization_hazard()


def flush_and_hard_exit(code: int) -> NoReturn:
    """Flush stdio + loguru sinks, then ``os._exit`` past interpreter finalization."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (ValueError, OSError):
        pass
    try:
        from loguru import logger

        logger.remove()
    except Exception:
        pass
    os._exit(code & 0xFF)
