# -*- coding: utf-8 -*-
"""Path-confinement guard for author-provided DAG file references."""

import posixpath

from ._errors import DagValidationError


def check_confined(path: str, *, what: str) -> None:
    """Reject an absolute or workdir-escaping reference path.

    Author-provided file references (``ref:`` / ``ref_path:`` and file
    inputs ``{"file": ...}``) must be relative paths that stay inside the
    session workdir. An absolute path or a ``..`` component that escapes
    the root would let an (auto-run, LLM-authored) graph read arbitrary
    host files, so such paths are rejected up front.

    Args:
        path (`str`):
            The reference path taken from a node spec.
        what (`str`):
            A short label used in the error message (e.g. ``"ref"``).

    Raises:
        `DagValidationError`:
            When ``path`` is absolute or escapes the workdir root.
    """
    normalized = posixpath.normpath(path)
    if posixpath.isabs(path) or path.startswith("\\") or normalized == ".." or normalized.startswith("../"):
        raise DagValidationError(
            f"{what} path '{path}' must be relative to and within the session workdir",
        )
