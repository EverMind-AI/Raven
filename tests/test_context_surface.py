"""The turn's front end reaches the agent's context.

``Source.surface`` has carried this since the served page began declaring itself
at handshake, but nothing downstream read it: the runtime block reported only
the channel, and the page shares the ``tui`` channel with the terminal on
purpose. So an agent answering a browser reader offered to exit with Ctrl+C.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from raven.context_engine.assembler import ContextAssembler
from raven.context_engine.base import AssemblyContext, TokenBudget
from raven.context_engine.curator import TurnContext
from raven.context_engine.segments import render

NOW = lambda: datetime(2026, 8, 21, 15, 0)  # noqa: E731


def _budget() -> TokenBudget:
    return TokenBudget(
        context_length=200_000,
        reserved_output=8000,
        reserved_tools=4000,
        reserved_system=4000,
        available_history=184_000,
    )


# ── the rendered block ────────────────────────────────────────────


def test_a_declared_surface_is_reported_next_to_the_channel() -> None:
    """Next to, not instead of: the channel is where a reply is delivered, the
    surface is what the reader is looking at."""
    block = render.build_runtime_context(NOW, "tui", "default", surface="page")

    assert "Channel: tui" in block
    assert "Surface: page" in block


def test_the_page_is_described_as_having_no_terminal() -> None:
    """The whole point. A bare word does not stop the agent offering Ctrl+C --
    it has to say there is no terminal."""
    block = render.build_runtime_context(NOW, "tui", "default", surface="page")
    line = next(ln for ln in block.splitlines() if ln.startswith("Surface:"))

    assert "browser" in line
    assert "no terminal" in line


def test_the_terminal_is_described_as_a_terminal() -> None:
    block = render.build_runtime_context(NOW, "tui", "default", surface="tui")
    assert "terminal" in next(ln for ln in block.splitlines() if ln.startswith("Surface:"))


def test_the_desktop_shell_also_says_there_is_no_terminal() -> None:
    """It wraps the same page, so the same advice is wrong there."""
    block = render.build_runtime_context(NOW, "tui", "default", surface="shell")
    assert "no terminal" in next(ln for ln in block.splitlines() if ln.startswith("Surface:"))


def test_an_undeclared_surface_adds_no_line() -> None:
    """A transport that declares nothing (an IM channel, a cron turn) must not
    grow a line that says nothing."""
    block = render.build_runtime_context(NOW, "telegram", "c1")
    assert "Surface:" not in block


def test_an_unknown_surface_is_still_named() -> None:
    """A front end this build has no gloss for is reported rather than dropped:
    the name alone is more than the agent had."""
    block = render.build_runtime_context(NOW, "tui", "default", surface="wkwebview")
    assert "Surface: wkwebview" in block


# ── the chain from the turn ───────────────────────────────────────


@pytest.mark.parametrize("surface", ["page", "shell", "tui", None])
async def test_the_assembler_carries_the_turn_surface_into_the_block(surface) -> None:
    """The seam this change exists to close: TurnContext -> AssemblyContext ->
    the rendered block."""
    assembler = ContextAssembler([], get_tool_definitions=lambda: [])
    assembled = await assembler.assemble(
        "tui:default",
        [],
        _budget(),
        turn=TurnContext(current_message="hi", channel="tui", chat_id="default", surface=surface),
    )
    text = "\n".join(str(m.get("content") or "") for m in assembled.messages)

    if surface is None:
        assert "Surface:" not in text
    else:
        assert f"Surface: {surface}" in text


async def test_the_context_defaults_to_no_surface() -> None:
    """Every other builder constructs an AssemblyContext without one; adding a
    required field would have broken all of them."""
    ctx = AssemblyContext(
        session_key="s",
        current_message="hi",
        media=None,
        channel="tui",
        chat_id="default",
        session_messages=[],
        budget=_budget(),
    )
    assert ctx.surface is None
