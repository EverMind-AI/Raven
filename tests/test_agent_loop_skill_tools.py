"""Registration of the skill retrieval tools in ``AgentLoop.__init__``.

``read_skill`` and ``use_skill`` both resolve ``local/`` and ``everos/`` ids
from the on-disk registry, so neither needs a Skill Hub endpoint to be useful.
That matters beyond symmetry: a skill declaring ``inject: description`` is
advertised by a digest entry that prints no body and is excluded from BM25
routing, so the tool the digest names is the only route to its instructions.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import TurnPolicy
from raven.agent.loop.wiring import WiringMixin
from raven.config.raven import HubSourceConfig, SkillForgeRouterConfig
from raven.providers.base import LLMProvider, LLMResponse


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

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
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def loop():
    with tempfile.TemporaryDirectory() as td:
        yield AgentLoop(provider=_StubProvider(), workspace=Path(td), model="stub", policy=TurnPolicy(max_iterations=2))


def test_both_skill_tools_register_without_a_hub(loop) -> None:
    assert loop._skill_hub_client is None, "this construction configures no Hub"
    assert getattr(loop.context.skills, "registry", None) is not None
    assert loop.tools.has("read_skill"), "gating read_skill on a Hub client hides the local body route"
    assert loop.tools.has("use_skill")


def test_the_hub_client_is_built_while_switched_off(tmp_path: Path) -> None:
    """The client is built whenever an endpoint exists, even with the switch
    off, so turning the source back on needs no loop rebuild. The tools ask
    the live config before each Hub call; only the endpoint needs a reload."""
    off = SkillForgeRouterConfig(hub=HubSourceConfig(endpoint="http://hub.test", enabled=False))

    client = WiringMixin._build_skill_hub_client(tmp_path, off)
    assert client is not None
    asyncio.run(client.aclose())

    assert (
        WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig(hub=HubSourceConfig(endpoint=None)))
        is None
    )


def test_find_skill_offers_the_hub_library_only_while_the_switch_is_on(tmp_path: Path, monkeypatch) -> None:
    """The schema the model receives, through the real registry: a loop built
    with the switch off still holds a client, and turning the switch on or off
    must move the sentence without a rebuild."""
    import json

    from raven import home as raven_home
    from raven.agent.loop.bundles import EngineWiring

    config = tmp_path / "config.json"
    hub = {"endpoint": "http://hub.test", "enabled": False}
    config.write_text(json.dumps({"skillForge": {"router": {"hub": hub}}}), encoding="utf-8")
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: config)
    raven_home.set_config_path(config)
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path / "ws",
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        engine=EngineWiring(skill_forge_router_config=SkillForgeRouterConfig(hub=HubSourceConfig(**hub))),
    )

    def described() -> str:
        return next(
            d["function"]["description"] for d in loop.tools.get_definitions() if d["function"]["name"] == "find_skill"
        )

    try:
        assert loop._skill_hub_client is not None
        assert "110k" not in described()
        config.write_text(json.dumps({"skillForge": {"router": {"hub": {**hub, "enabled": True}}}}), encoding="utf-8")
        assert "110k" in described()
    finally:
        asyncio.run(loop._skill_hub_client.aclose())


def test_the_digest_names_a_tool_this_loop_registered(loop) -> None:
    """Pins the two halves together: a description-mode skill's body is in no
    other segment, so an unregistered tool in its hint is a dead end the agent
    cannot route around."""
    catalog = loop.context.skills
    metas = [m for m in catalog.get_always_skills() if getattr(m, "inject", "full") == "description"]
    assert metas, "the shipped orchestration guide is description-mode"

    hint = catalog.build_skill_digest(metas)
    named = set(re.findall(r"call `(\w+)\(", hint))
    assert named, hint
    for tool_name in named:
        assert loop.tools.has(tool_name), f"digest points at unregistered {tool_name!r}"
