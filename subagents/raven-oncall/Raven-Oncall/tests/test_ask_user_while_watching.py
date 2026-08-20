"""Asking while an experiment runs.

``ask_user`` waits ten minutes and does nothing else while it waits. Measured
2026-08-19: a campaign's command was wrong, the loop diagnosed it correctly --
"the ops system already links staged_case into job_dir, so this cp has the same
file as source and target" -- and asked here. A machine sat idle and a 25-minute
budget ran, for a question that blocked nothing: it could have declared a fresh
campaign with the command fixed and asked at the same time.

``ops_ask_owner`` is the one with a choice about that, and it records the question
on the campaign either way.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ops_home(tmp_path, monkeypatch):
    import raven.agent.tools.ops as ops_mod

    home = tmp_path / "ops"
    home.mkdir()
    monkeypatch.setattr(ops_mod, "_ops_home", lambda: home)
    return home


@pytest.mark.asyncio
async def test_a_live_campaign_sends_the_question_to_ops_ask_owner(ops_home):
    from raven.agent.tools.ask_user import AskUserTool

    (ops_home / "beam").mkdir()

    out = await AskUserTool().execute(questions=[{"question": "which way?"}])

    assert out.startswith("Error") and "ops_ask_owner" in out
    assert "beam" in out, "naming the experiment says why this arrived"
    assert "blocks_progress" in out
    assert "Nothing was asked" in out, "it must be clear the owner saw nothing"


@pytest.mark.asyncio
async def test_a_concluded_campaign_does_not_count(ops_home):
    # Finished work is not something a question can stall.
    from raven.agent.tools.ask_user import AskUserTool

    (ops_home / "done").mkdir()
    (ops_home / "done" / "concluded.json").write_text("{}", encoding="utf-8")

    out = await AskUserTool().execute(questions=[{"question": "which way?"}])

    assert "ops_ask_owner" not in out


@pytest.mark.asyncio
async def test_no_campaigns_at_all_leaves_ask_user_alone(ops_home):
    from raven.agent.tools.ask_user import AskUserTool

    out = await AskUserTool().execute(questions=[{"question": "which way?"}])

    assert "ops_ask_owner" not in out


@pytest.mark.asyncio
async def test_an_unreadable_ops_home_leaves_ask_user_alone(monkeypatch):
    """Asking the owner something must not depend on the ops layer being readable."""
    import raven.agent.tools.ops as ops_mod
    from raven.agent.tools.ask_user import AskUserTool

    def explode():
        raise RuntimeError("ops home is gone")

    monkeypatch.setattr(ops_mod, "_ops_home", explode)

    out = await AskUserTool().execute(questions=[{"question": "which way?"}])

    assert "ops_ask_owner" not in out
