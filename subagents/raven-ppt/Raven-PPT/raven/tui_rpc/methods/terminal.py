"""``terminal.resize`` RPC handler — record cols, return ok.

ui-tui's ``useMainApp.ts:426`` calls ``terminal.resize`` with the new
``{cols, rows}`` payload whenever Ink observes a SIGWINCH; the call is
fire-and-forget. We need a handler that:

  1. Never raises (so the SIGWINCH burst doesn't spam errors), and
  2. Optionally records the latest ``cols`` so ``cli.dispatch`` can use it
     as the Rich console width on its next invocation (a future-proofing
     hook — ``cli_dispatch.py`` does not yet read this value, but the wire
     is in place for v0.2).

The recorded state is module-level (a single TUI subprocess has exactly one
terminal, so a singleton is correct).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher


# Module-level latest-known terminal size. ``None`` means "no resize event
# observed yet"; callers should fall back to ``shutil.get_terminal_size()``
# or a sensible default (80 cols) in that case.
_LATEST_COLS: int | None = None
_LATEST_ROWS: int | None = None


def get_latest_cols() -> int | None:
    """Return the most recently reported terminal column count, or ``None``.

    Future consumer: ``cli_dispatch.py`` may read this to size the Rich
    Console it injects into in-process CLI calls.
    """
    return _LATEST_COLS


def get_latest_rows() -> int | None:
    """Return the most recently reported terminal row count, or ``None``."""
    return _LATEST_ROWS


def _coerce_dim(value: Any) -> int | None:
    """Return ``value`` as a positive int, else ``None``."""
    if isinstance(value, bool):
        # bool is a subclass of int — reject explicitly.
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


async def terminal_resize(params: dict) -> dict:
    """``terminal.resize`` — record dimensions, return ``{ok: true}``.

    Accepts ``{cols, rows}`` (both optional positive ints). Anything else is
    silently coerced to a no-op record — we never raise here because the
    upstream SIGWINCH burst would otherwise flood error frames.
    """
    global _LATEST_COLS, _LATEST_ROWS
    if isinstance(params, dict):
        cols = _coerce_dim(params.get("cols"))
        rows = _coerce_dim(params.get("rows"))
        if cols is not None:
            _LATEST_COLS = cols
        if rows is not None:
            _LATEST_ROWS = rows
    return {"ok": True}


def register_terminal_methods(dispatcher: "Dispatcher") -> None:
    """Register ``terminal.resize`` on a dispatcher instance."""
    dispatcher.register("terminal.resize", terminal_resize)


__all__ = [
    "terminal_resize",
    "register_terminal_methods",
    "get_latest_cols",
    "get_latest_rows",
]
