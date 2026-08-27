"""Hard-exit past CPython interpreter finalization.

Finalizing the interpreter while native state is still live can segfault
(``Py_FinalizeEx``; SIGSEGV, exit 139) and mask the real exit code. The one-shot
agent uses this helper after its explicit async teardown, and the pytest session
hook uses it on CI after the suite has recorded its status.
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
