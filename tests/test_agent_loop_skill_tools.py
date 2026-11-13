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


def test_both_skill_tools_register_with_a_default_config(loop) -> None:
    assert loop._skill_hub_client is not None, "the loop always carries a Hub client"
    assert getattr(loop.context.skills, "registry", None) is not None
    assert loop.tools.has("read_skill"), "the body route does not depend on the Hub being on"
    assert loop.tools.has("use_skill")


def test_the_hub_client_is_built_while_switched_off(tmp_path: Path) -> None:
    """The client is built whatever the switch says, so turning the source
    back on needs no loop rebuild. The tools ask the live config before each
    Hub call; the URL is the shared marketplace unless one is configured."""
    off = SkillForgeRouterConfig(hub=HubSourceConfig(endpoint="http://hub.test", enabled=False))

    client = WiringMixin._build_skill_hub_client(tmp_path, off)
    assert client is not None
    asyncio.run(client.aclose())

    unset = WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig(hub=HubSourceConfig(endpoint=None)))
    assert unset is not None
    assert unset._base == "https://skillhub.evermind.ai"
    asyncio.run(unset.aclose())


def test_the_hub_client_follows_the_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_SKILLHUB_URL", "https://staging.hub.test/")

    client = WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig())
    assert client is not None
    assert client._base == "https://staging.hub.test"
    asyncio.run(client.aclose())


def test_an_invalid_env_override_disables_the_client(tmp_path: Path, monkeypatch) -> None:
    """The operator named a destination and pointed away from the marketplace;
    the only safe answer to a refused override is no client at all."""
    monkeypatch.setenv("RAVEN_SKILLHUB_URL", "http://staging.hub.test")

    assert (
        WiringMixin._build_skill_hub_client(
            tmp_path, SkillForgeRouterConfig(hub=HubSourceConfig(endpoint="https://hub.internal.test"))
        )
        is None
    )
    assert WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig()) is None


def test_a_valid_env_override_wins_over_a_configured_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_SKILLHUB_URL", "https://staging.hub.test")

    client = WiringMixin._build_skill_hub_client(
        tmp_path, SkillForgeRouterConfig(hub=HubSourceConfig(endpoint="https://hub.internal.test"))
    )
    assert client is not None
    assert client._base == "https://staging.hub.test"
    asyncio.run(client.aclose())


def test_a_key_without_a_configured_endpoint_is_not_sent_to_the_fallback(tmp_path: Path) -> None:
    """The endpoint has a schema default, so a truthy value does not mean the
    operator named one. A key written without an endpoint belongs to some
    other Hub; carrying it to the marketplace would disclose it."""
    client = WiringMixin._build_skill_hub_client(
        tmp_path, SkillForgeRouterConfig(hub=HubSourceConfig(api_key="legacy-secret"))
    )
    assert client is not None
    assert client._base == "https://skillhub.evermind.ai"
    assert client._api_key is None
    asyncio.run(client.aclose())


def test_a_key_follows_an_explicitly_configured_endpoint(tmp_path: Path) -> None:
    client = WiringMixin._build_skill_hub_client(
        tmp_path,
        SkillForgeRouterConfig(hub=HubSourceConfig(endpoint="https://hub.internal.test", api_key="k")),
    )
    assert client is not None
    assert (client._base, client._api_key) == ("https://hub.internal.test", "k")
    asyncio.run(client.aclose())


def test_a_key_loaded_without_an_endpoint_is_not_attached(tmp_path: Path) -> None:
    """The loader tracks which fields the file supplied, and that is the signal
    the client uses: a default endpoint is not a configured one."""
    import json

    from raven.config.raven import load_raven_config

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"skillForge": {"router": {"hub": {"apiKey": "legacy-secret"}}}}), encoding="utf-8")
    hub = load_raven_config(cfg).skill_forge.router.hub

    client = WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig(hub=hub))

    assert client is not None
    assert client._api_key is None
    asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "payload",
    [
        {"skillForge": {"router": {"hub": {"endpoint": "https://hub.internal.test", "apiKey": "k"}}}},
        {"skill_forge": {"router": {"hub": {"endpoint": "https://hub.internal.test", "apiKey": "k"}}}},
        {"skillRouter": {"hub": {"endpoint": "https://hub.internal.test", "apiKey": "k"}}},
    ],
    ids=["camel", "snake", "legacy-top-level-router"],
)
def test_a_key_survives_every_spelling_of_an_explicit_endpoint(tmp_path: Path, payload: dict) -> None:
    """The loader nests a legacy top-level router in memory, so the endpoint is
    still the file's: the guard must not drop the key for those shapes."""
    import json

    from raven.config.raven import load_raven_config

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    hub = load_raven_config(cfg).skill_forge.router.hub

    client = WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig(hub=hub))

    assert client is not None
    assert (client._base, client._api_key) == ("https://hub.internal.test", "k")
    asyncio.run(client.aclose())


def test_a_legacy_null_endpoint_does_not_carry_its_key(tmp_path: Path) -> None:
    import json

    from raven.config.raven import load_raven_config

    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"skillForge": {"router": {"hub": {"endpoint": None, "apiKey": "legacy-secret"}}}}),
        encoding="utf-8",
    )
    hub = load_raven_config(cfg).skill_forge.router.hub

    client = WiringMixin._build_skill_hub_client(tmp_path, SkillForgeRouterConfig(hub=hub))

    assert client is not None
    assert client._api_key is None
    asyncio.run(client.aclose())


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
