"""Completion gates end-to-end through the loop: injection, counting, silence.

The pure decision helpers are unit-tested in ``test_completion_gates``; this
covers the wiring that a real run depends on -- the nudge actually reaching the
message list, the counter reaching TurnOutcome, and the gate staying silent
once the turn has edited a file. Waiting for a live task to finish empty is not
a reliable way to exercise the firing path.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.providers.base import LLMProvider, LLMResponse
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest

# Every guardrail switch the loop reads. Clearing a partial list would let a
# developer or CI shell that exports one of them turn a "default behavior"
# test into something else entirely.
ALL_GATE_SWITCHES = (
    "RAVEN_REQUIRE_REAL_TEST_EVIDENCE",
    "RAVEN_VERIFY_BEFORE_COMPLETE",
    "RAVEN_GATE_STALE",
    "RAVEN_GATE_RED",
    "RAVEN_GATE_EMPTY_DIFF",
)


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _TextOnlyProvider(LLMProvider):
    """Answers with text and never calls a tool -- a turn that edits nothing."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        self.calls += 1
        self.seen.append(list(messages))
        return LLMResponse(content="The repository already satisfies this.", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


def _agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=False,
    )


def _clear_gate_env(monkeypatch) -> None:
    """Unset every switch so the run exercises the built-in defaults."""
    for switch in ALL_GATE_SWITCHES:
        monkeypatch.delenv(switch, raising=False)


def _set_all_gates(monkeypatch, value: str) -> None:
    for switch in ALL_GATE_SWITCHES:
        monkeypatch.setenv(switch, value)


def _only_empty_diff_gate(monkeypatch) -> None:
    _set_all_gates(monkeypatch, "off")
    monkeypatch.setenv("RAVEN_GATE_EMPTY_DIFF", "1")


def _user_turn(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


@pytest.mark.asyncio
async def test_finishing_without_edits_injects_the_nudge_once_and_counts_it(workspace, monkeypatch):
    _only_empty_diff_gate(monkeypatch)
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _final, _tools, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "Make sure the frobnicator handles empty input."}],
    )

    injected = [
        m for m in messages
        if m.get("role") == "user" and "without any observed repository modification" in str(m.get("content"))
    ]
    assert len(injected) == 1
    assert outcome.gate_triggers == {"empty_diff": 1}
    assert provider.calls == 2  # the answer, then one more after the nudge


@pytest.mark.asyncio
async def test_second_turn_does_not_claim_the_first_turn_s_edits_never_happened(workspace, monkeypatch):
    """Regression: a harness turn that only confirms completion must not be
    told the repository is unmodified when turn one already changed it.

    Reproduces the WorkBuddy shape exactly -- the eval re-invokes the agent for
    a second turn whenever the first reply misses the completion token, so the
    edits sit in turn one and the turn-local edit flag starts over at False.
    """
    _only_empty_diff_gate(monkeypatch)
    _init_repo(workspace)
    (workspace / "app.py").write_text("x = 2  # the previous turn's fix\n", encoding="utf-8")
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "Continue from the current state."}],
    )

    assert not any("without any observed repository modification" in str(m.get("content")) for m in messages)
    assert outcome.gate_triggers == {}
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_gate_still_fires_when_the_repository_is_verifiably_untouched(workspace, monkeypatch):
    _only_empty_diff_gate(monkeypatch)
    _init_repo(workspace)
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop(
        [{"role": "user", "content": "Make sure the frobnicator handles empty input."}],
    )

    injected = [
        m for m in messages
        if m.get("role") == "user" and "without any observed repository modification" in str(m.get("content"))
    ]
    assert len(injected) == 1
    assert outcome.gate_triggers == {"empty_diff": 1}


def _init_repo(path: Path) -> None:
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"),
    ):
        subprocess.run(("git", *args), cwd=path, check=True, env=env, capture_output=True)


@pytest.mark.asyncio
async def test_gate_is_silent_when_switched_off(workspace, monkeypatch):
    _set_all_gates(monkeypatch, "off")
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    _f, _t, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "hi"}])

    assert not any("without any observed repository modification" in str(m.get("content")) for m in messages)
    assert outcome.gate_triggers == {}
    assert provider.calls == 1


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.asyncio
async def test_no_gate_arms_itself_by_default(workspace, monkeypatch, interactive):
    """Swarm-integration default: gates are opt-in in both run shapes. A
    one-shot worker also serves non-coding requests (explain code, follow-up
    Q&A turns) where change-the-code gates only mis-fire."""
    _clear_gate_env(monkeypatch)
    provider = _TextOnlyProvider()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=10,
        restrict_to_workspace=True,
        interactive=interactive,
    )

    _f, _t, messages, outcome = await agent._run_agent_loop([{"role": "user", "content": "explain this repo"}])

    assert not any("without any observed repository modification" in str(m.get("content")) for m in messages)
    assert outcome.gate_triggers == {}
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_stdout_stays_clean_when_no_gate_fires(workspace, monkeypatch, capsys):
    """An orchestrator consumes one-shot stdout as the reply; quiet turns must
    not append a gate_triggers trailer line."""
    _clear_gate_env(monkeypatch)
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    out = await agent._process_message(_user_turn("explain this repo"), session_key="s1")

    assert out is not None
    assert "gate_triggers" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_stdout_reports_counters_when_a_gate_fires(workspace, monkeypatch, capsys):
    _only_empty_diff_gate(monkeypatch)
    provider = _TextOnlyProvider()
    agent = _agent(workspace, provider)

    out = await agent._process_message(
        _user_turn("Make sure the frobnicator handles empty input."), session_key="s1"
    )

    assert out is not None
    assert "gate_triggers: empty_diff=1" in capsys.readouterr().out
