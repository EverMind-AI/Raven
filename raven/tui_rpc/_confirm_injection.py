"""Monkey-patch helper that bridges ``typer.confirm`` into the ConfirmBroker.

Mirrors ``_console_injection``: a context
manager that temporarily replaces ``typer.confirm`` / ``click.confirm`` so the
7 destructive CLI call sites stay untouched while the prompt is answered over
RPC instead of read from the (non-TTY, EOF-immediately) dispatch stdin.

``typer.confirm is click.confirm`` (same object), but they are distinct module
attributes — both are patched and both restored in ``finally``.

Only entered when a ConfirmBroker is present (TUI production path). Outside the
context, ``typer.confirm`` retains its native real-TTY behavior, so the pure
CLI path is unchanged.

The bridge runs in the dispatch worker thread (``asyncio.to_thread``); it hands
the coroutine to the server's event loop via ``run_coroutine_threadsafe`` and
blocks on ``.result()``. The broker's hard limit guarantees ``.result()``
returns, so no extra timeout is needed here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import click
import typer

if TYPE_CHECKING:
    from raven.tui_rpc.confirm_broker import ConfirmBroker


@contextlib.contextmanager
def confirm_injection(broker: "ConfirmBroker", loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Redirect ``typer.confirm`` / ``click.confirm`` to ``broker`` for the block."""
    orig_typer_confirm = typer.confirm
    orig_click_confirm = click.confirm

    def _bridged_confirm(
        text: Any = "",
        default: bool = False,
        abort: bool = False,
        *_args: Any,
        **_kwargs: Any,
    ) -> bool:
        future = asyncio.run_coroutine_threadsafe(broker.await_confirm(str(text), default=bool(default)), loop)
        answer = future.result()
        if abort and not answer:
            # Preserve click's abort=True contract (no current call site uses
            # it, but keep the bridge faithful to the original signature).
            raise click.exceptions.Abort()
        return answer

    typer.confirm = _bridged_confirm
    click.confirm = _bridged_confirm
    try:
        yield
    finally:
        typer.confirm = orig_typer_confirm
        click.confirm = orig_click_confirm


__all__ = ["confirm_injection"]
