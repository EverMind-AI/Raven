"""Platform probes shared by tests that depend on POSIX filesystem semantics."""

from __future__ import annotations

import os


def chmod_is_enforced() -> bool:
    """Whether chmod actually restricts filesystem access for this process.

    Root bypasses the permission bits, and Windows has no ``geteuid`` and
    does not block the access either, so neither can exercise a test that
    depends on a refused read or write.
    """
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() != 0
