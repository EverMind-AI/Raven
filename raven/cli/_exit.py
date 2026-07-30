"""Hard-exit past CPython interpreter finalization.

Finalizing the interpreter while native state is still live can segfault
(``Py_FinalizeEx``; SIGSEGV, exit 139) and mask the real exit code. The only
caller today is the pytest session hook (``tests/conftest.py``), where a fully
green run was observed exiting 139 on Linux.

This used to also gate ``raven.cli.commands.run`` on a live lancedb background
thread. That gate is gone: nothing under ``raven/`` imports lancedb -- memory
talks to everos over HTTP and everos runs out-of-process -- so the probe could
not fire in any configuration.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn


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
