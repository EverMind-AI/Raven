"""Placeholder for chat E2E integration tests — DEFERRED to production wire-up.

The chat infrastructure (turn.* / SubscriptionEmitter / chat_stream) is all
testable, but the `raven tui` production startup path is not yet wired to
instantiate AgentLoop + SubscriptionEmitter. That wire (and the JS side
``chatStream.attach()`` flip) lives in the follow-up production wire-up.

When that ships, replace ``@pytest.mark.skip`` below with the 3 E2E
scenarios (short chat / long chat / Ctrl+C cancel).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason=(
        "production wire deferred to tui-chat-prodwire L2 v0.1.1 — "
        "see docs/openspec/changes/tui-chat/proposal.md §4.2 amendment "
        "2026-05-19 'B 路径' decision"
    ),
)


def test_chat_e2e_short_placeholder() -> None:
    """Placeholder — see module docstring."""
    raise NotImplementedError("activated by tui-chat-prodwire L2")


def test_chat_e2e_long_placeholder() -> None:
    """Placeholder — see module docstring."""
    raise NotImplementedError("activated by tui-chat-prodwire L2")


def test_chat_e2e_ctrl_c_placeholder() -> None:
    """Placeholder — see module docstring."""
    raise NotImplementedError("activated by tui-chat-prodwire L2")
