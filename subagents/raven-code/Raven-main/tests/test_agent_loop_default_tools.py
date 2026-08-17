"""What is NOT on the default tool surface, and why.

The web group (web_search / web_fetch / deep_research offer) is opt-in: a coding
agent runs against a checkout, and benchmark sandboxes are offline, so every web
call there fails and only teaches the model a dead path. ``cron`` is gone for a
different reason -- scheduling reminders is not coding work, and its schema is
the largest of any tool. These tests pin both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent.manager import SubagentManager
from raven.config.schema import DeepResearchToolConfig, WebToolsConfig

WEB_TOOLS = ("web_search", "web_fetch", "deep_research")


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _loop(tmp_path: Path, **kwargs) -> AgentLoop:
    return AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        deep_research_config=DeepResearchToolConfig(),
        **kwargs,
    )


def test_web_tools_absent_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _loop(tmp_path, brave_api_key="test-key", jina_api_key="test-key")
    for name in WEB_TOOLS:
        assert not loop.tools.has(name), f"{name} must not be registered by default"


def test_web_tools_registered_when_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MIROTHINKER_API_KEY", raising=False)
    loop = _loop(tmp_path, web_enabled=True, brave_api_key="test-key")
    for name in WEB_TOOLS:
        assert loop.tools.has(name), f"{name} must come back when web tools are enabled"


def test_web_search_still_needs_its_key_when_enabled(tmp_path: Path) -> None:
    loop = _loop(tmp_path, web_enabled=True, brave_api_key=None)
    assert not loop.tools.has("web_search"), "the group switch must not bypass the API-key gate"
    assert loop.tools.has("web_fetch"), "web_fetch is keyless and rides the group switch alone"


def test_web_config_defaults_to_enabled() -> None:
    # Swarm-integration line: an orchestrated worker researches as well as
    # codes, so the config default is on; offline/evaluation configs pin false.
    assert WebToolsConfig().enabled is True


def test_cron_is_not_registered_even_with_a_service(tmp_path: Path) -> None:
    # A CronService is still wired by the gateway and the TUI so scheduled jobs
    # keep firing; what went away is the agent's ability to add jobs itself.
    from raven.proactive_engine.schedulers.cron.service import CronService

    service = CronService(tmp_path / "jobs.json")
    loop = _loop(tmp_path, cron_service=service)
    assert loop.cron_service is service, "the service must still be reachable for the proactive stack"
    assert not loop.tools.has("cron"), "cron must not be on the default tool surface"


@pytest.mark.asyncio
async def test_subagent_inherits_the_switch(tmp_path: Path) -> None:
    manager = SubagentManager(provider=_StubProvider(), workspace=tmp_path, model="stub")
    assert manager.web_enabled is False, "subagents default to no web tools"
    loop = _loop(tmp_path, web_enabled=True)
    assert loop.subagents.web_enabled is True, "the loop must pass its switch down to subagents"
