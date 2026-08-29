"""``terminal.resize`` RPC handler — record cols, return ok.

ui-tui's ``useMainApp.ts`` calls ``terminal.resize`` with the new
``{cols, rows}`` payload whenever Ink observes a SIGWINCH; the call is
fire-and-forget. We need a handler that:

  1. Never raises (so the SIGWINCH burst doesn't spam errors), and
  2. Records the latest ``cols`` / ``rows``, which no raven code reads back
     today -- a console that wants the width still calls
     ``shutil.get_terminal_size()``.

The recorded state is module-level (a single TUI subprocess has exactly one
terminal, so a singleton is correct).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


# Module-level latest-known terminal size. ``None`` means "no resize event
# observed yet"; callers should fall back to ``shutil.get_terminal_size()``
# or a sensible default (80 cols) in that case.
_LATEST_COLS: int | None = None
_LATEST_ROWS: int | None = None


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
]
