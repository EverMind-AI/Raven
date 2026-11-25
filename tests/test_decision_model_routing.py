"""Unit tests for decision model routing across skills and specialist classification."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from raven.agent.decision_model import (
    is_system1_decision_model,
    resolve_decision_binding,
    select_skills_with_decision_model,
    select_specialist_with_decision_model,
)
from raven.agent.subagent.backends import AgentMeta
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.dag_tool import SubAgentDagTool
from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.spawn_tool import SpawnTool
from raven.agent.tools.skill_hub import FindSkillTool
from raven.config.live import LiveConfig, decision_for_skills, decision_for_specialists
from raven.context_engine.scent import _SILENT, ScentMenu
from raven.providers.binding import ModelBinding


@dataclass
class _StubReply:
    content: str | None
    finish_reason: str = "stop"


class _StubProvider:
    def __init__(
        self, reply: str | None = None, *, error: Exception | None = None, finish_reason: str = "stop"
    ) -> None:
        self._reply = reply
        self._error = error
        self._finish_reason = finish_reason
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "main-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> _StubReply:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._error is not None:
            raise self._error
        return _StubReply(content=self._reply, finish_reason=self._finish_reason)


class _StubPool:
    def __init__(self, bindings: dict[tuple[str, str | None], ModelBinding | None]) -> None:
        self._bindings = bindings
        self.requests: list[tuple[str, str | None]] = []

    def bind_pin(self, model: str, provider_name: str | None = None) -> ModelBinding | None:
        self.requests.append((model, provider_name))
        return self._bindings.get((model, provider_name))


@dataclass
class _StubHit:
    qualified_id: str
    name: str
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class _StubRouter:
    def __init__(self, hits: list[_StubHit]) -> None:
        self._hits = hits
        self.queries: list[tuple[str, int]] = []

    async def select(self, query: str, *, history: list[Any], k: int = 3) -> list[_StubHit]:
        self.queries.append((query, k))
        return list(self._hits[:k])


def _write_live_config(tmp_path: Path, data: dict[str, Any]) -> LiveConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return LiveConfig(path)


# ---------------------------------------------------------------- system 1 filter


ACCEPTED = [
    "microsoft-decision-1", "Microsoft-decision-1", "ghe-copilot/microsoft-decision-1",
    "ghe-copilot/microsoft-decision-1-1", "gemini/gemini-decision-1", "Gemini-decision-1",
    "gpt-6-luna-decisions", "github-copilot/gpt-6-luna-decisions", "custom/decisions",
    "decisions", "e2e-decision-1",
]
REJECTED = [
    "", "   ", "ghe-copilot/gpt-4o-mini", "gpt-4o-mini", "gpt-4.1-nano", "claude-haiku-4-5",
    "claude-haiku-5.5", "gemini-2.5-flash", "mai-code-2-flash", "custom/lite", "o1", "o1-mini",
    "o3-mini", "o4-mini", "deepseek-r1", "gemini-2.5-flash-thinking", "qwq-32b", "marco-o1",
    "claude-sonnet-4-6", "claude-opus-4-6", "gpt-4o", "gpt-5", "gemini-2.5-pro",
]


@pytest.mark.parametrize("model_id", ACCEPTED)
def test_is_system1_decision_model_accepts_dedicated_decision_models(model_id: str) -> None:
    assert is_system1_decision_model(model_id) is True


@pytest.mark.parametrize("model_id", REJECTED)
def test_is_system1_decision_model_rejects_everything_else(model_id: str) -> None:
    assert is_system1_decision_model(model_id) is False


def test_is_system1_decision_model_rejects_when_reasoning_flag_true() -> None:
    assert is_system1_decision_model("microsoft-decision-1", reasoning=True) is False
    assert is_system1_decision_model("microsoft-decision-1", reasoning=False) is True


# ---------------------------------------------------------------- resolver


def test_resolve_decision_binding_returns_bound_model(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider()
    expected = ModelBinding(provider=decision_provider, model="microsoft-decision-1")
    pool = _StubPool({("microsoft-decision-1", "openai"): expected})

    resolved = resolve_decision_binding(live, pool)
    assert resolved is expected
    assert pool.requests == [("microsoft-decision-1", "openai")]


@pytest.mark.parametrize("bad_model", [m for m in REJECTED if m.strip()])
def test_resolve_decision_binding_returns_none_for_non_system1_model(tmp_path: Path, bad_model: str) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": bad_model, "provider": "openai", "useForSkills": True}},
    )
    expected = ModelBinding(provider=_StubProvider(), model=bad_model)
    pool = _StubPool({(bad_model, "openai"): expected})

    assert resolve_decision_binding(live, pool) is None
    assert pool.requests == []


def test_resolve_decision_binding_returns_none_when_unconfigured(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"useForSkills": True}})
    pool = _StubPool({})

    assert resolve_decision_binding(live, pool) is None
    assert pool.requests == []


def test_resolve_decision_binding_returns_none_without_pool(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai"}})

    assert resolve_decision_binding(live, None) is None
    assert resolve_decision_binding(None, _StubPool({})) is None


def test_resolve_decision_binding_returns_none_when_credentials_unusable(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai"}})
    pool = _StubPool({("microsoft-decision-1", "openai"): None})

    assert resolve_decision_binding(live, pool) is None
    assert pool.requests == [("microsoft-decision-1", "openai")]


# ---------------------------------------------------------------- skill helper


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_filters_and_orders() -> None:
    provider = _StubProvider('["hub/sql-opt", "hub/not-real", "local/testing", "hub/sql-opt"]')
    binding = ModelBinding(provider=provider, model="microsoft-decision-1")
    candidates = [
        ("local/testing", "Testing methodology"),
        ("hub/sql-opt", "Optimize SQL queries"),
        ("hub/docker", "Container tips"),
    ]

    result = await select_skills_with_decision_model(binding, "tune slow postgres query", candidates, max_skills=2)

    assert result == ["hub/sql-opt", "local/testing"]
    assert len(provider.calls) == 1
    assert provider.calls[0]["model"] == "microsoft-decision-1"


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_accepts_fenced_json() -> None:
    provider = _StubProvider('```json\n["hub/sql-opt"]\n```')
    binding = ModelBinding(provider=provider, model="microsoft-decision-1")

    result = await select_skills_with_decision_model(
        binding,
        "tune slow postgres query",
        [("hub/sql-opt", "Optimize SQL queries")],
        max_skills=3,
    )
    assert result == ["hub/sql-opt"]


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_returns_none_on_error_or_bad_json() -> None:
    candidates = [("hub/sql-opt", "Optimize SQL queries")]

    err_binding = ModelBinding(provider=_StubProvider(error=RuntimeError("boom")), model="microsoft-decision-1")
    assert await select_skills_with_decision_model(err_binding, "sql", candidates) is None

    bad_binding = ModelBinding(provider=_StubProvider("not json at all"), model="microsoft-decision-1")
    assert await select_skills_with_decision_model(bad_binding, "sql", candidates) is None

    obj_binding = ModelBinding(provider=_StubProvider('{"ids": ["hub/sql-opt"]}'), model="microsoft-decision-1")
    assert await select_skills_with_decision_model(obj_binding, "sql", candidates) is None


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_timeout_returns_none() -> None:
    binding = ModelBinding(provider=_StubProvider(error=asyncio.TimeoutError()), model="microsoft-decision-1")
    assert (
        await select_skills_with_decision_model(
            binding,
            "sql",
            [("hub/sql-opt", "Optimize SQL queries")],
        )
        is None
    )


# ---------------------------------------------------------------- ScentMenu + FindSkillTool


def _sample_hits() -> list[_StubHit]:
    return [
        _StubHit("local/alpha", "alpha", meta={"description": "Alpha generic helper"}),
        _StubHit("hub/sql-opt", "sql-opt", meta={"description": "Optimize SQL queries"}),
        _StubHit("hub/python-debug", "python-debug", meta={"description": "Debug Python tracebacks"}),
    ]


@pytest.mark.asyncio
async def test_scent_menu_uses_decision_model_when_toggle_on(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider('["hub/python-debug", "local/alpha"]')
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    menu = ScentMenu(
        router,
        top_k=2,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

    assert result.skill_ids == ["hub/python-debug", "local/alpha"]
    assert len(decision_provider.calls) == 1
    assert decision_provider.calls[0]["model"] == "microsoft-decision-1"


@pytest.mark.asyncio
async def test_scent_menu_skips_decision_model_when_toggle_off(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": False}},
    )
    decision_provider = _StubProvider('["hub/python-debug"]')
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    menu = ScentMenu(
        router,
        top_k=2,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

    assert result.skill_ids == ["local/alpha", "hub/sql-opt"]
    assert decision_provider.calls == []
    assert pool.requests == []


@pytest.mark.asyncio
async def test_scent_menu_skips_decision_model_when_unconfigured(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"useForSkills": True}})
    pool = _StubPool({})
    router = _StubRouter(_sample_hits())
    menu = ScentMenu(
        router,
        top_k=2,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

    assert result.skill_ids == ["local/alpha", "hub/sql-opt"]
    assert pool.requests == []


@pytest.mark.asyncio
async def test_scent_menu_falls_back_on_decision_model_error_or_bad_json(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    for provider in (_StubProvider(error=RuntimeError("network down")), _StubProvider("not-json")):
        pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=provider, model="microsoft-decision-1")})
        router = _StubRouter(_sample_hits())
        menu = ScentMenu(
            router,
            top_k=2,
            decision_binding=lambda p=pool: resolve_decision_binding(live, p) if decision_for_skills(live) else None,
        )

        result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

        assert result.skill_ids == ["local/alpha", "hub/sql-opt"]
        assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_scent_menu_returns_silent_when_decision_model_returns_empty_list(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider("[]")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    menu = ScentMenu(
        router,
        top_k=2,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

    assert result == _SILENT
    assert not result
    assert result.skill_ids == []
    assert len(decision_provider.calls) == 1


@pytest.mark.asyncio
async def test_find_skill_tool_uses_decision_model_and_falls_back(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider('["hub/python-debug"]')
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    tool = FindSkillTool(
        lambda: router,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    output = await tool.execute(query="debug python traceback")

    assert "hub/python-debug" in output
    assert "local/alpha" not in output
    assert len(decision_provider.calls) == 1

    bad_provider = _StubProvider("invalid-json")
    bad_pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=bad_provider, model="microsoft-decision-1")})
    fallback_tool = FindSkillTool(
        lambda: router,
        decision_binding=lambda: resolve_decision_binding(live, bad_pool) if decision_for_skills(live) else None,
    )
    fallback_output = await fallback_tool.execute(query="debug python traceback")

    assert "local/alpha" in fallback_output
    assert "hub/sql-opt" in fallback_output
    assert "hub/python-debug" in fallback_output


@pytest.mark.asyncio
async def test_find_skill_tool_skips_decision_model_when_toggle_off_or_unconfigured(tmp_path: Path) -> None:
    live_off = _write_live_config(
        tmp_path / "off",
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": False}},
    )
    off_provider = _StubProvider('["hub/python-debug"]')
    off_pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=off_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    off_tool = FindSkillTool(
        lambda: router,
        decision_binding=lambda: resolve_decision_binding(live_off, off_pool) if decision_for_skills(live_off) else None,
    )

    off_output = await off_tool.execute(query="debug python traceback")

    assert off_provider.calls == []
    assert off_pool.requests == []
    assert "local/alpha" in off_output
    assert "hub/sql-opt" in off_output
    assert "hub/python-debug" in off_output

    live_unconfigured = _write_live_config(tmp_path / "unconfigured", {"decisionModel": {"useForSkills": True}})
    unconfigured_pool = _StubPool({})
    unconfigured_tool = FindSkillTool(
        lambda: router,
        decision_binding=lambda: (
            resolve_decision_binding(live_unconfigured, unconfigured_pool)
            if decision_for_skills(live_unconfigured)
            else None
        ),
    )

    unconfigured_output = await unconfigured_tool.execute(query="debug python traceback")

    assert unconfigured_pool.requests == []
    assert "local/alpha" in unconfigured_output
    assert "hub/sql-opt" in unconfigured_output
    assert "hub/python-debug" in unconfigured_output


@pytest.mark.asyncio
async def test_find_skill_tool_falls_back_to_router_hits_when_decision_model_returns_empty(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider("[]")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    router = _StubRouter(_sample_hits())
    tool = FindSkillTool(
        lambda: router,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    output = await tool.execute(query="debug python traceback")

    assert len(decision_provider.calls) == 1
    assert "No matching skills found." not in output
    assert "local/alpha" in output
    assert "hub/sql-opt" in output
    assert "hub/python-debug" in output


# ---------------------------------------------------------------- specialist routing


@pytest.mark.asyncio
async def test_select_specialist_with_decision_model_validates_choice() -> None:
    binding = ModelBinding(provider=_StubProvider("`coder`"), model="microsoft-decision-1")
    choice = await select_specialist_with_decision_model(
        binding,
        "fix failing tests",
        "- coder: writes code\n- writer: writes prose",
        ["coder", "writer", "default"],
    )
    assert choice == "coder"

    unknown_binding = ModelBinding(provider=_StubProvider("hallucinated"), model="microsoft-decision-1")
    assert (
        await select_specialist_with_decision_model(
            unknown_binding,
            "fix failing tests",
            "- coder: writes code",
            ["coder", "default"],
        )
        is None
    )

    none_binding = ModelBinding(provider=_StubProvider("NONE"), model="microsoft-decision-1")
    assert (
        await select_specialist_with_decision_model(
            none_binding,
            "fix failing tests",
            "- coder: writes code",
            ["coder", "default"],
        )
        is None
    )


@pytest.mark.asyncio
async def test_select_specialist_with_decision_model_timeout_returns_none() -> None:
    binding = ModelBinding(provider=_StubProvider(error=asyncio.TimeoutError()), model="microsoft-decision-1")
    assert (
        await select_specialist_with_decision_model(
            binding,
            "fix failing tests",
            "- coder: writes code",
            ["coder", "default"],
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["coder.", "coder\n(best fit)"])
async def test_select_specialist_with_decision_model_strips_punctuation_and_extra_lines(reply: str) -> None:
    binding = ModelBinding(provider=_StubProvider(reply), model="microsoft-decision-1")
    choice = await select_specialist_with_decision_model(
        binding,
        "fix failing tests",
        "- coder: writes code\n- writer: writes prose",
        ["coder", "writer", "default"],
    )
    assert choice == "coder"


def _make_manager(
    tmp_path: Path,
    *,
    main_provider: _StubProvider,
    pool: _StubPool | None,
    live: LiveConfig | None,
) -> SubagentManager:
    return SubagentManager(
        provider=main_provider,
        workspace=tmp_path,
        model="main-model",
        provider_pool=pool,
        live_config=live,
    )


@pytest.mark.asyncio
async def test_subagent_classify_uses_decision_model_when_toggle_on(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": True}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

    result = await manager._classify(
        [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
        "Refactor the parser and add tests",
        "general",
    )

    assert decision_for_specialists(live) is True
    assert result == "coder"
    assert len(decision_provider.calls) == 1
    assert decision_provider.calls[0]["model"] == "microsoft-decision-1"
    assert main_provider.calls == []


@pytest.mark.asyncio
async def test_subagent_classify_uses_main_provider_when_toggle_off(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": False}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

    result = await manager._classify(
        [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
        "Write release notes",
        "general",
    )

    assert result == "writer"
    assert decision_provider.calls == []
    assert len(main_provider.calls) == 1
    assert main_provider.calls[0]["model"] == "main-model"


@pytest.mark.asyncio
async def test_subagent_classify_falls_back_when_unconfigured(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"useForSpecialists": True}})
    main_provider = _StubProvider("coder")
    pool = _StubPool({})
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

    result = await manager._classify(
        [("coder", "Handles code changes")],
        "Fix bug",
        "general",
    )

    assert result == "coder"
    assert pool.requests == []
    assert len(main_provider.calls) == 1
    assert main_provider.calls[0]["model"] == "main-model"


@pytest.mark.asyncio
async def test_subagent_classify_falls_back_on_decision_model_error_or_invalid_name(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": True}},
    )
    for decision_provider in (_StubProvider(error=RuntimeError("timeout")), _StubProvider("not-a-specialist")):
        main_provider = _StubProvider("writer")
        pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
        manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

        result = await manager._classify(
            [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
            "Draft changelog",
            "general",
        )

        assert result == "writer"
        assert len(decision_provider.calls) == 1
        assert len(main_provider.calls) == 1
        assert main_provider.calls[0]["model"] == "main-model"


@pytest.mark.asyncio
async def test_subagent_classify_returns_default_from_decision_model_without_main_provider(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": True}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("general")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

    result = await manager._classify(
        [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
        "Answer a general question",
        "general",
    )

    assert result == "general"
    assert len(decision_provider.calls) == 1
    assert main_provider.calls == []


@pytest.mark.asyncio
async def test_subagent_classify_with_live_config_none_falls_back_cleanly(tmp_path: Path) -> None:
    main_provider = _StubProvider("coder")
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=None, live=None)

    result = await manager._classify(
        [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
        "Refactor helper module",
        "general",
    )

    assert result == "coder"
    assert len(main_provider.calls) == 1
    assert main_provider.calls[0]["model"] == "main-model"


# ---------------------------------------------------------------- SpawnTool + SubAgentDagTool auto-routing


class _StubSpawnManager:
    def __init__(self, workspace: Path, agents: list[AgentMeta]) -> None:
        self.workspace = workspace
        self._agents = agents
        self.spawn_calls: list[dict[str, Any]] = []

    def list_agents(self) -> list[AgentMeta]:
        return list(self._agents)

    def session_dir_for(self, session_key: str) -> Path:
        safe = session_key.replace(":", "_")
        path = self.workspace / "sessions" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def spawn(self, **kwargs: Any) -> str:
        self.spawn_calls.append(kwargs)
        return f"Subagent [{kwargs.get('task_summary', '')}] started on {kwargs.get('agent')}."


class _StubDagRegistry:
    def __init__(self, agents: list[AgentMeta]) -> None:
        self._agents = agents

    def names(self) -> list[str]:
        return sorted(a.name for a in self._agents)

    def roster_text(self) -> str:
        return "\n".join(f"- {a.name}: {a.description}" for a in self._agents)


def _sample_agent_metas() -> list[AgentMeta]:
    return [
        AgentMeta(name="coder", description="Handles code changes", stateful=False, reads_local_files=True),
        AgentMeta(name="writer", description="Drafts documentation", stateful=False, reads_local_files=True),
        AgentMeta(name=GENERIC_AGENT, description="General purpose fallback", stateful=False, reads_local_files=True),
    ]


@pytest.mark.asyncio
async def test_spawn_tool_auto_routing_when_enabled(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": True}},
    )
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    manager = _StubSpawnManager(tmp_path, _sample_agent_metas())
    tool = SpawnTool(
        manager,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_specialists(live) else None,
    )

    schema = tool.parameters
    assert "auto" in schema["properties"]["subagent"]["enum"]
    assert "subagent" not in schema["required"]

    await tool.execute(
        task_summary="Fix parser bug",
        prompt_template="Update parser and add tests.",
        node_id="fix-auto",
        subagent="auto",
    )
    await tool.execute(
        task_summary="Fix lexer bug",
        prompt_template="Update lexer tokens.",
        node_id="fix-omitted",
    )
    assert len(decision_provider.calls) == 2
    assert [call["agent"] for call in manager.spawn_calls] == ["coder", "coder"]

    await tool.execute(
        task_summary="Draft docs",
        prompt_template="Write release notes.",
        node_id="explicit-writer",
        subagent="writer",
    )
    assert len(decision_provider.calls) == 2
    assert manager.spawn_calls[-1]["agent"] == "writer"

    none_provider = _StubProvider("NONE")
    none_pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=none_provider, model="microsoft-decision-1")})
    fallback_tool = SpawnTool(
        manager,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live, none_pool) if decision_for_specialists(live) else None,
    )
    await fallback_tool.execute(
        task_summary="Ambiguous task",
        prompt_template="Handle miscellaneous request.",
        node_id="fallback-generic",
        subagent="auto",
    )
    assert len(none_provider.calls) == 1
    assert manager.spawn_calls[-1]["agent"] == GENERIC_AGENT


def test_spawn_tool_schema_without_auto_routing(tmp_path: Path) -> None:
    manager = _StubSpawnManager(tmp_path, _sample_agent_metas())
    live_off = _write_live_config(
        tmp_path / "off",
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": False}},
    )
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=_StubProvider("coder"), model="microsoft-decision-1")})

    tool_off = SpawnTool(
        manager,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live_off, pool) if decision_for_specialists(live_off) else None,
    )
    schema_off = tool_off.parameters
    assert "auto" not in schema_off["properties"]["subagent"]["enum"]
    assert "subagent" in schema_off["required"]

    tool_no_binding = SpawnTool(manager, decision_binding=None)  # type: ignore[arg-type]
    schema_no_binding = tool_no_binding.parameters
    assert "auto" not in schema_no_binding["properties"]["subagent"]["enum"]
    assert "subagent" in schema_no_binding["required"]


@pytest.mark.asyncio
async def test_dag_tool_auto_routing_schema_and_resolution(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": True}},
    )
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("microsoft-decision-1", "openai"): ModelBinding(provider=decision_provider, model="microsoft-decision-1")})
    registry = _StubDagRegistry(_sample_agent_metas())
    tool = SubAgentDagTool(
        workspace=tmp_path,
        registry=registry,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_specialists(live) else None,
    )

    node_schema = tool.node_schema()
    assert "auto" in node_schema["properties"]["subagent"]["enum"]
    assert "subagent" not in node_schema["required"]

    nodes = [
        {"id": "n1", "subagent": "auto", "node_summary": "Fix code", "prompt_template": "Patch module."},
        {"id": "n2", "node_summary": "Add tests", "prompt_template": "Write unit tests."},
        {"id": "n3", "subagent": "writer", "node_summary": "Update docs", "prompt_template": "Write changelog."},
    ]
    resolved = await tool._resolve_auto_subagents(nodes, task_summary="Ship release")

    assert [node["subagent"] for node in resolved] == ["coder", "coder", "writer"]
    assert len(decision_provider.calls) == 2

    live_off = _write_live_config(
        tmp_path / "off",
        {"decisionModel": {"model": "microsoft-decision-1", "provider": "openai", "useForSpecialists": False}},
    )
    tool_off = SubAgentDagTool(
        workspace=tmp_path,
        registry=registry,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live_off, pool) if decision_for_specialists(live_off) else None,
    )
    off_node_schema = tool_off.node_schema()
    assert "auto" not in off_node_schema["properties"]["subagent"]["enum"]
    assert "subagent" in off_node_schema["required"]


@pytest.mark.asyncio
async def test_finish_reason_error_falls_back_to_none_and_warns() -> None:
    from loguru import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        # Valid-looking content proves the error reason, not parsing, causes the fallback.
        skill = ModelBinding(
            provider=_StubProvider('["hub/sql-opt"]', finish_reason="error"), model="microsoft-decision-1"
        )
        assert await select_skills_with_decision_model(skill, "sql", [("hub/sql-opt", "SQL")]) is None
        spec = ModelBinding(provider=_StubProvider("coder", finish_reason="error"), model="microsoft-decision-1")
        assert await select_specialist_with_decision_model(spec, "t", "- coder: x", ["coder"]) is None
    finally:
        logger.remove(sink)
    joined = " ".join(msgs)
    assert "skill selection returned finish_reason=error; falling back" in joined
    assert "specialist selection returned finish_reason=error; falling back" in joined


# ---------------------------------------------------------------- native Decision API


class _NamedProvider(_StubProvider):
    def __init__(self, name: str, api_key: str = "gk") -> None:
        super().__init__("unused")
        self.provider_name = name
        self.api_key = api_key
        self.api_base = None


def _install_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[Any]:
    import httpx

    import raven.agent.decision_model as dm

    seen: list[Any] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        dm, "_new_client", lambda timeout, verify=True: httpx.AsyncClient(transport=httpx.MockTransport(_handle), timeout=timeout)
    )
    monkeypatch.setattr(dm, "_copilot_credentials", lambda: ("cop-key", "https://api.enterprise.githubcopilot.com"))
    return seen


def _map_answer(choice: str, probs: dict[str, float]) -> dict[str, Any]:
    return {"answers": {"q1": {"type": "choice", "choice": choice, "confidence": 1.0, "probabilities": probs}}}


def _array_answer(choice: str, probs: dict[str, float]) -> dict[str, Any]:
    return {
        "answers": [
            {
                "type": "choice", "name": "q1", "choice": choice, "confidence": 1.0,
                "probabilities": [{"value": k, "probability": v} for k, v in probs.items()],
            }
        ]
    }


def test_decision_protocol_routing() -> None:
    from raven.agent.decision_model import _decision_protocol as proto

    assert proto("gemini", "gemini/gemini-decision-1") == "gemini_decisions"
    assert proto("github_copilot", "github-copilot/microsoft-decision-1") == "copilot_systemone"
    assert proto("github_copilot", "microsoft-decision-1-1") == "copilot_systemone"
    assert proto("github_copilot", "gpt-6-luna-decisions") == "copilot_decisions"
    assert proto("custom", "decisions") == "chat"
    assert proto("", "microsoft-decision-1") == "chat"


_CANDS = [("hub/a", "desc A"), ("hub/b", "desc B"), ("hub/c", "desc C")]


@pytest.mark.asyncio
async def test_gemini_decisions_skill_request_and_multi_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen = _install_http(
        monkeypatch,
        lambda r: httpx.Response(200, json=_map_answer("hub/a", {"hub/a": 0.6, "hub/b": 0.3, "hub/c": 0.1})),
    )
    binding = ModelBinding(provider=_NamedProvider("gemini"), model="gemini/gemini-decision-1")
    got = await select_skills_with_decision_model(binding, "query", _CANDS, max_skills=3)
    assert got == ["hub/a", "hub/b"]
    req = seen[0]
    assert str(req.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
    assert req.headers["x-goog-api-key"] == "gk"
    body = json.loads(req.content)
    assert body["model"] == "gemini-decision-1" and body["input"] == "query"
    crit = body["questions"]["q1"]["criteria"]
    assert body["questions"]["q1"]["type"] == "choice"
    assert list(crit)[0] == "__none__" and set(crit) == {"__none__", "hub/a", "hub/b", "hub/c"}


@pytest.mark.asyncio
async def test_gemini_decisions_caps_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    _install_http(
        monkeypatch,
        lambda r: httpx.Response(200, json=_map_answer("hub/a", {"hub/a": 0.4, "hub/b": 0.3, "hub/c": 0.3})),
    )
    binding = ModelBinding(provider=_NamedProvider("gemini"), model="gemini-decision-1")
    assert await select_skills_with_decision_model(binding, "q", _CANDS, max_skills=2) == ["hub/a", "hub/b"]
    _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("__none__", {"__none__": 1.0})))
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == []


@pytest.mark.asyncio
async def test_copilot_systemone_uses_map_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("coder", {"coder": 0.99})))
    binding = ModelBinding(
        provider=_NamedProvider("github_copilot"), model="github-copilot/microsoft-decision-1"
    )
    got = await select_specialist_with_decision_model(binding, "fix", "- coder: code", ["coder", "default"])
    assert got == "coder"
    req = seen[0]
    assert str(req.url) == "https://api.enterprise.githubcopilot.com/v1/systemone"
    assert req.headers["authorization"] == "Bearer cop-key"
    body = json.loads(req.content)
    assert body["model"] == "microsoft-decision-1"
    assert "input" not in body
    assert "fix" in body["questions"]["q1"]["instructions"]
    assert set(body["questions"]["q1"]["criteria"]) == {"__none__", "coder", "default"}


@pytest.mark.asyncio
async def test_copilot_decisions_uses_array_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen = _install_http(
        monkeypatch,
        lambda r: httpx.Response(200, json=_array_answer("hub/b", {"hub/b": 0.7, "hub/a": 0.28, "__none__": 0.02})),
    )
    binding = ModelBinding(
        provider=_NamedProvider("github_copilot"), model="github-copilot/gpt-6-luna-decisions"
    )
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == ["hub/b", "hub/a"]
    req = seen[0]
    assert str(req.url) == "https://api.enterprise.githubcopilot.com/v1/decisions"
    body = json.loads(req.content)
    assert body["model"] == "gpt-6-luna-decisions"
    q = body["questions"][0]
    assert q["name"] == "q1" and q["type"] == "choice"
    assert [c["value"] for c in q["choices"]] == ["__none__", "hub/a", "hub/b", "hub/c"]

    _install_http(monkeypatch, lambda r: httpx.Response(200, json=_array_answer("__none__", {"__none__": 1.0})))
    assert await select_specialist_with_decision_model(binding, "t", "- coder", ["coder"]) is None


@pytest.mark.asyncio
async def test_native_decision_http_error_and_timeout_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from loguru import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    binding = ModelBinding(provider=_NamedProvider("gemini"), model="gemini-decision-1")
    try:
        _install_http(monkeypatch, lambda r: httpx.Response(400, json={"error": "model_not_supported"}))
        assert await select_skills_with_decision_model(binding, "q", _CANDS) is None
        assert await select_specialist_with_decision_model(binding, "t", "- coder", ["coder"]) is None

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        _install_http(monkeypatch, boom)
        assert await select_skills_with_decision_model(binding, "q", _CANDS, timeout=2.0) is None

        _install_http(monkeypatch, lambda r: httpx.Response(200, json={"nope": 1}))
        assert await select_skills_with_decision_model(binding, "q", _CANDS) is None
    finally:
        logger.remove(sink)
    joined = " ".join(msgs)
    assert "gemini_decisions request failed (HTTP 400); falling back" in joined
    assert "gemini_decisions request timed out after 2.0s; falling back" in joined


class _Inner:
    provider_name = "gemini"

    def __init__(self, api_key: str = "ik", api_base: str | None = None, tls_verify: bool = True) -> None:
        self.api_key = api_key
        self.api_base = api_base
        self.tls_verify = tls_verify


class _Rotor(_StubProvider):
    """Stand-in for EndpointRotorProvider: no key of its own, keys live on _inners."""

    def __init__(self, inners: list[Any]) -> None:
        super().__init__("unused")
        self.provider_name = "gemini"
        self._inners = inners


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_base", "expected"),
    [
        ("https://gw.example/v1beta", "https://gw.example/v1beta/decisions"),
        ("https://gw.example/v1beta/", "https://gw.example/v1beta/decisions"),
        ("https://gw.example", "https://gw.example/v1beta/decisions"),
    ],
)
async def test_gemini_decisions_api_base_joining(monkeypatch: pytest.MonkeyPatch, api_base: str, expected: str) -> None:
    import httpx

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("hub/a", {"hub/a": 1.0})))
    prov = _NamedProvider("gemini")
    prov.api_base = api_base
    binding = ModelBinding(provider=prov, model="gemini-decision-1")
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == ["hub/a"]
    assert str(seen[0].url) == expected


@pytest.mark.asyncio
async def test_gemini_decisions_unwraps_rotor_and_passes_tls_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    import raven.agent.decision_model as dm

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("hub/a", {"hub/a": 1.0})))
    verifies: list[Any] = []
    orig = dm._new_client

    def spy(timeout: float, verify: bool = True) -> Any:
        verifies.append(verify)
        return orig(timeout, verify)

    monkeypatch.setattr(dm, "_new_client", spy)
    inner = _Inner(api_key="inner-key", api_base="https://gw.example/v1beta", tls_verify=False)
    binding = ModelBinding(provider=_Rotor([_Inner(api_key=""), inner]), model="gemini-decision-1")
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == ["hub/a"]
    assert seen[0].headers["x-goog-api-key"] == "inner-key"
    assert str(seen[0].url) == "https://gw.example/v1beta/decisions"
    assert verifies == [False]


@pytest.mark.asyncio
async def test_gemini_decisions_empty_key_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("hub/a", {})))
    for prov in (_NamedProvider("gemini", api_key=""), _Rotor([_Inner(api_key="")])):
        binding = ModelBinding(provider=prov, model="gemini-decision-1")
        assert await select_skills_with_decision_model(binding, "q", _CANDS) is None
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"answers": []},
        {"answers": {}},
        {"answers": {"q1": {"type": "choice", "choice": "not-a-candidate", "probabilities": {}}}},
        {"answers": {"q1": {"type": "choice", "choice": 5}}},
        {"answers": {"q1": {"type": "choice", "choice": "hub/a", "probabilities": {"hub/b": "high"}}}},
    ],
)
async def test_parse_answer_edge_cases_fall_back(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    import httpx

    _install_http(monkeypatch, lambda r: httpx.Response(200, json=payload))
    binding = ModelBinding(provider=_NamedProvider("gemini"), model="gemini-decision-1")
    assert await select_skills_with_decision_model(binding, "q", _CANDS) is None
    assert await select_specialist_with_decision_model(binding, "t", "- coder", ["coder"]) is None


@pytest.mark.asyncio
async def test_parse_answer_null_probabilities_keeps_top_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    _install_http(
        monkeypatch,
        lambda r: httpx.Response(200, json={"answers": {"q1": {"choice": "hub/a", "probabilities": None}}}),
    )
    binding = ModelBinding(provider=_NamedProvider("gemini"), model="gemini-decision-1")
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == ["hub/a"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "route", "answer"),
    [
        ("microsoft-decision-1", "systemone", _map_answer("hub/a", {"hub/a": 1.0})),
        ("gpt-6-luna-decisions", "decisions", _array_answer("hub/a", {"hub/a": 1.0})),
    ],
)
async def test_copilot_base_none_falls_back_to_default_base(
    monkeypatch: pytest.MonkeyPatch, model: str, route: str, answer: dict[str, Any]
) -> None:
    import httpx

    import raven.agent.decision_model as dm

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=answer))
    monkeypatch.setattr(dm, "_copilot_credentials", lambda: ("cop-key", None))
    binding = ModelBinding(provider=_NamedProvider("github_copilot"), model=model)
    assert await select_skills_with_decision_model(binding, "q", _CANDS) == ["hub/a"]
    assert str(seen[0].url) == f"{dm._COPILOT_BASE}/v1/{route}"


@pytest.mark.asyncio
async def test_copilot_credentials_timeout_and_empty_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import httpx

    import raven.agent.decision_model as dm

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_map_answer("hub/a", {})))
    binding = ModelBinding(provider=_NamedProvider("github_copilot"), model="microsoft-decision-1")

    def slow() -> tuple[str, str | None]:
        time.sleep(0.5)
        return "k", None

    monkeypatch.setattr(dm, "_copilot_credentials", slow)
    assert await select_skills_with_decision_model(binding, "q", _CANDS, timeout=0.05) is None

    monkeypatch.setattr(dm, "_copilot_credentials", lambda: ("", None))
    assert await select_skills_with_decision_model(binding, "q", _CANDS) is None
    assert seen == []


@pytest.mark.asyncio
async def test_native_options_dedupe_exclude_none_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    seen = _install_http(monkeypatch, lambda r: httpx.Response(200, json=_array_answer("hub/a", {"hub/a": 1.0})))
    binding = ModelBinding(provider=_NamedProvider("github_copilot"), model="gpt-6-luna-decisions")

    dupes = [("hub/a", "A"), ("hub/b", "B"), ("hub/a", "A again"), ("__none__", "clash")]
    assert await select_skills_with_decision_model(binding, "q", dupes) == ["hub/a"]
    values = [c["value"] for c in json.loads(seen[0].content)["questions"][0]["choices"]]
    assert values == ["__none__", "hub/a", "hub/b"]

    only_none = [("__none__", "clash"), ("__none__", "again")]
    assert await select_skills_with_decision_model(binding, "q", only_none) == []
    assert await select_specialist_with_decision_model(binding, "t", "r", ["__none__"]) is None
    assert len(seen) == 1

    many = [(f"s{i}", f"d{i}") for i in range(300)]
    await select_skills_with_decision_model(binding, "q", many)
    values = [c["value"] for c in json.loads(seen[1].content)["questions"][0]["choices"]]
    assert len(values) == 255 and values[0] == "__none__" and values[-1] == "s253"
