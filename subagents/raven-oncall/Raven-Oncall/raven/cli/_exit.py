"""Hard-exit past CPython interpreter finalization for native-unsafe runtimes.

Building the agent loop opens a lancedb-backed store, and lancedb starts a
process-global ``LanceDBBackgroundEventLoop`` (Rust/tokio) daemon thread with
no public shutdown hook. Finalizing the interpreter while that native runtime
is still live segfaults (``Py_FinalizeEx``; SIGSEGV, exit 139), masking the
command's real exit code. Every raven command that builds the loop starts that
thread, so the guard lives once at the CLI exit chokepoint
(:func:`raven.cli.commands.run`): when the hazard is live, flush and
``os._exit`` past finalization.

CliRunner invokes the Typer ``app`` object directly (in-process), never the
console-script ``run`` wrapper, so test hosts keep normal exit semantics.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import NoReturn


def lancedb_finalization_hazard() -> bool:
    """Whether lancedb's Rust/tokio background thread is live in this process.

    Merely importing lancedb is safe — the thread only exists once a connection
    is opened — so key on the live thread, not the imported module.
    """
    return any(t.name == "LanceDBBackgroundEventLoop" for t in threading.enumerate())


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
