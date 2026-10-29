"""Only the Raven host keeps a Task Board: where the tool is registered, and where it is not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.task_board.service import TaskBoardService
from raven.agent.task_board.tool import TaskBoardTool
from raven.config.schema import AgentDefaults, TaskBoardConfig
from raven.providers.base import LLMProvider, LLMResponse

REPO = Path(__file__).resolve().parents[1]


class _StubProvider(LLMProvider):
    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


def _loop(workspace: Path, engine: EngineWiring | None = None) -> AgentLoop:
    """A loop wired the way the runtime wires it: with `agents.defaults.taskBoard`."""
    return AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=engine if engine is not None else EngineWiring(task_board_config=TaskBoardConfig()),
    )


def test_a_loop_built_without_the_config_keeps_no_board(tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    agent = AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )

    assert agent.tools.get("task_board") is None and agent.task_board is None


def test_the_host_registers_the_board_and_its_service(tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    agent = _loop(tmp_path)

    assert isinstance(agent.tools.get("task_board"), TaskBoardTool)
    assert isinstance(agent.task_board, TaskBoardService)


def test_a_sub_agent_process_has_neither(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")

    agent = _loop(tmp_path)

    assert agent.tools.get("task_board") is None
    assert agent.task_board is None


def test_a_disabled_board_has_neither(tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    agent = _loop(tmp_path, EngineWiring(task_board_config=TaskBoardConfig(enabled=False)))

    assert agent.tools.get("task_board") is None
    assert agent.task_board is None


def test_every_product_config_switches_the_board_off():
    configs = sorted((REPO / "agents").glob("*/config.json"))

    assert len(configs) >= 5
    for path in configs:
        defaults = json.loads(path.read_text(encoding="utf-8"))["agents"]["defaults"]
        assert defaults["taskBoard"]["enabled"] is False, path


def test_the_config_reads_camel_case_and_refuses_minimal_effort():
    defaults = AgentDefaults.model_validate({"taskBoard": {"enabled": False, "reasoningEffort": "medium"}})

    assert defaults.task_board == TaskBoardConfig(enabled=False, reasoning_effort="medium")
    assert AgentDefaults().task_board == TaskBoardConfig(enabled=True, reasoning_effort="low")
    with pytest.raises(ValidationError):
        TaskBoardConfig(reasoning_effort="minimal")


def test_the_turn_binds_the_board_to_its_session(tmp_path, monkeypatch):
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)
    agent = _loop(tmp_path)

    agent._set_tool_context("web", "alpha", None, session_key="web:alpha")

    assert agent.tools.get("task_board")._session_key.get() == "web:alpha"
