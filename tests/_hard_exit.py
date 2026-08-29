"""Hard-exit past CPython interpreter finalization, for the pytest session hook.

Finalizing the interpreter while native state is still live can segfault
(``Py_FinalizeEx``; SIGSEGV, exit 139) and mask the real exit code; a fully
green run was observed exiting 139 on Linux. The hook in ``conftest.py`` routes
the recorded status through here on CI.
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
