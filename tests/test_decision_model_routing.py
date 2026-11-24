"""Unit tests for decision model routing across skills and specialist classification."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from raven.agent.decision_model import (
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


class _StubProvider:
    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
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
        return _StubReply(content=self._reply)


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


# ---------------------------------------------------------------- resolver


def test_resolve_decision_binding_returns_bound_model(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider()
    expected = ModelBinding(provider=decision_provider, model="fast-judge")
    pool = _StubPool({("fast-judge", "openai"): expected})

    resolved = resolve_decision_binding(live, pool)
    assert resolved is expected
    assert pool.requests == [("fast-judge", "openai")]


def test_resolve_decision_binding_returns_none_when_unconfigured(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"useForSkills": True}})
    pool = _StubPool({})

    assert resolve_decision_binding(live, pool) is None
    assert pool.requests == []


def test_resolve_decision_binding_returns_none_without_pool(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"model": "fast-judge", "provider": "openai"}})

    assert resolve_decision_binding(live, None) is None
    assert resolve_decision_binding(None, _StubPool({})) is None


def test_resolve_decision_binding_returns_none_when_credentials_unusable(tmp_path: Path) -> None:
    live = _write_live_config(tmp_path, {"decisionModel": {"model": "fast-judge", "provider": "openai"}})
    pool = _StubPool({("fast-judge", "openai"): None})

    assert resolve_decision_binding(live, pool) is None
    assert pool.requests == [("fast-judge", "openai")]


# ---------------------------------------------------------------- skill helper


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_filters_and_orders() -> None:
    provider = _StubProvider('["hub/sql-opt", "hub/not-real", "local/testing", "hub/sql-opt"]')
    binding = ModelBinding(provider=provider, model="fast-judge")
    candidates = [
        ("local/testing", "Testing methodology"),
        ("hub/sql-opt", "Optimize SQL queries"),
        ("hub/docker", "Container tips"),
    ]

    result = await select_skills_with_decision_model(binding, "tune slow postgres query", candidates, max_skills=2)

    assert result == ["hub/sql-opt", "local/testing"]
    assert len(provider.calls) == 1
    assert provider.calls[0]["model"] == "fast-judge"


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_accepts_fenced_json() -> None:
    provider = _StubProvider('```json\n["hub/sql-opt"]\n```')
    binding = ModelBinding(provider=provider, model="fast-judge")

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

    err_binding = ModelBinding(provider=_StubProvider(error=RuntimeError("boom")), model="fast-judge")
    assert await select_skills_with_decision_model(err_binding, "sql", candidates) is None

    bad_binding = ModelBinding(provider=_StubProvider("not json at all"), model="fast-judge")
    assert await select_skills_with_decision_model(bad_binding, "sql", candidates) is None

    obj_binding = ModelBinding(provider=_StubProvider('{"ids": ["hub/sql-opt"]}'), model="fast-judge")
    assert await select_skills_with_decision_model(obj_binding, "sql", candidates) is None


@pytest.mark.asyncio
async def test_select_skills_with_decision_model_timeout_returns_none() -> None:
    binding = ModelBinding(provider=_StubProvider(error=asyncio.TimeoutError()), model="fast-judge")
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider('["hub/python-debug", "local/alpha"]')
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
    router = _StubRouter(_sample_hits())
    menu = ScentMenu(
        router,
        top_k=2,
        decision_binding=lambda: resolve_decision_binding(live, pool) if decision_for_skills(live) else None,
    )

    result = await menu.build("Investigate why Python raises a stack overflow in worker pool", [])

    assert result.skill_ids == ["hub/python-debug", "local/alpha"]
    assert len(decision_provider.calls) == 1
    assert decision_provider.calls[0]["model"] == "fast-judge"


@pytest.mark.asyncio
async def test_scent_menu_skips_decision_model_when_toggle_off(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": False}},
    )
    decision_provider = _StubProvider('["hub/python-debug"]')
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    for provider in (_StubProvider(error=RuntimeError("network down")), _StubProvider("not-json")):
        pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider("[]")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider('["hub/python-debug"]')
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
    bad_pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=bad_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": False}},
    )
    off_provider = _StubProvider('["hub/python-debug"]')
    off_pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=off_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSkills": True}},
    )
    decision_provider = _StubProvider("[]")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
    binding = ModelBinding(provider=_StubProvider("`coder`"), model="fast-judge")
    choice = await select_specialist_with_decision_model(
        binding,
        "fix failing tests",
        "- coder: writes code\n- writer: writes prose",
        ["coder", "writer", "default"],
    )
    assert choice == "coder"

    unknown_binding = ModelBinding(provider=_StubProvider("hallucinated"), model="fast-judge")
    assert (
        await select_specialist_with_decision_model(
            unknown_binding,
            "fix failing tests",
            "- coder: writes code",
            ["coder", "default"],
        )
        is None
    )

    none_binding = ModelBinding(provider=_StubProvider("NONE"), model="fast-judge")
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
    binding = ModelBinding(provider=_StubProvider(error=asyncio.TimeoutError()), model="fast-judge")
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
    binding = ModelBinding(provider=_StubProvider(reply), model="fast-judge")
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": True}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
    manager = _make_manager(tmp_path, main_provider=main_provider, pool=pool, live=live)

    result = await manager._classify(
        [("coder", "Handles code changes"), ("writer", "Drafts documentation")],
        "Refactor the parser and add tests",
        "general",
    )

    assert decision_for_specialists(live) is True
    assert result == "coder"
    assert len(decision_provider.calls) == 1
    assert decision_provider.calls[0]["model"] == "fast-judge"
    assert main_provider.calls == []


@pytest.mark.asyncio
async def test_subagent_classify_uses_main_provider_when_toggle_off(tmp_path: Path) -> None:
    live = _write_live_config(
        tmp_path,
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": False}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": True}},
    )
    for decision_provider in (_StubProvider(error=RuntimeError("timeout")), _StubProvider("not-a-specialist")):
        main_provider = _StubProvider("writer")
        pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": True}},
    )
    main_provider = _StubProvider("writer")
    decision_provider = _StubProvider("general")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": True}},
    )
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
    none_pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=none_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": False}},
    )
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=_StubProvider("coder"), model="fast-judge")})

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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": True}},
    )
    decision_provider = _StubProvider("coder")
    pool = _StubPool({("fast-judge", "openai"): ModelBinding(provider=decision_provider, model="fast-judge")})
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
        {"decisionModel": {"model": "fast-judge", "provider": "openai", "useForSpecialists": False}},
    )
    tool_off = SubAgentDagTool(
        workspace=tmp_path,
        registry=registry,  # type: ignore[arg-type]
        decision_binding=lambda: resolve_decision_binding(live_off, pool) if decision_for_specialists(live_off) else None,
    )
    off_node_schema = tool_off.node_schema()
    assert "auto" not in off_node_schema["properties"]["subagent"]["enum"]
    assert "subagent" in off_node_schema["required"]
