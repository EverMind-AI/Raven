"""Registration rules for the web tools inside ``_register_default_tools``.

``web_search`` needs a Serper key. Registering it without one let the model
reach for a search it could not run, and the tool's error -- naming a config
file and an env var -- was relayed to whoever was on the other end of the
channel. It is withheld instead, on the same terms as the media tools right
below it.

``web_fetch`` is the contrast and is asserted alongside: it works with no key,
so it is registered unconditionally and must stay that way.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.web import WebSearchTool
from raven.contracts.tool import Tool
from raven.providers.base import LLMProvider, LLMResponse
from tests._wiring import wire


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
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _no_ambient_serper_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool falls back to ``SERPER_API_KEY``, so a developer who exports one
    would otherwise see these tests pass for the wrong reason."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)


def _loop(workspace: Path, **kw) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=workspace, model="stub", **wire(**kw))


def test_web_search_is_withheld_without_a_key(workspace) -> None:
    # Registered but withheld: registration stopped being the gate so a key
    # added while the process runs can surface the tool without a restart.
    loop = _loop(workspace)

    assert not loop.tools.offers_by_name("web_search"), (
        "offering a search that cannot run makes the model relay the tool's setup error to the user"
    )
    assert loop.tools.has("web_search"), "withheld, not unregistered -- the switch must stay reversible"
    assert loop.tools.offers_by_name("web_fetch"), "web_fetch needs no key and must stay unconditional"


def test_a_configured_key_offers_web_search(workspace) -> None:
    loop = _loop(workspace, search_api_key="sk-serper")

    assert loop.tools.offers_by_name("web_search")


def test_the_env_var_alone_offers_web_search(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool resolves its key at call time from the config value *or*
    # SERPER_API_KEY, so a gate that reads only the config would withdraw the
    # tool from a deploy that exports the variable and configures nothing.
    monkeypatch.setenv("SERPER_API_KEY", "sk-from-env")

    loop = _loop(workspace)

    assert loop.tools.offers_by_name("web_search")


#: Every layout a key can be added in, and the vendor selected while it is.
#: The canonical slot is what the settings page and the wizard write; the
#: pre-vendor leaf is what an unmigrated config still holds. A reader wired for
#: the leaf alone left the canonical case -- the common one -- withheld until
#: the next process, which no caller could tell from the tool being unkeyed.
_LIVE_KEY_LAYOUTS = [
    ("pre-vendor leaf", "serper", {"tools": {"web": {"search": {"apiKey": "sk-added-later"}}}}),
    (
        "canonical serper slot",
        "serper",
        {"tools": {"web": {"providers": {"serper": {"apiKey": "sk-added-later"}}}}},
    ),
    (
        "canonical slot of another vendor",
        "tavily",
        {
            "tools": {
                "web": {
                    "search": {"provider": "tavily"},
                    "providers": {"tavily": {"apiKey": "sk-added-later"}},
                }
            }
        },
    ),
]


@pytest.mark.parametrize(("layout", "vendor", "written"), _LIVE_KEY_LAYOUTS, ids=[c[0] for c in _LIVE_KEY_LAYOUTS])
def test_a_key_added_after_start_surfaces_web_search(
    workspace, tmp_path: Path, monkeypatch, layout: str, vendor: str, written: dict
) -> None:
    # The reversibility the registration gate could not give: the user edits the
    # config file, nothing re-registers, and the next assembly reads a different
    # answer -- both the veil and the credential the call then uses.
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(workspace, web_search_provider=vendor)
    assert not loop.tools.offers_by_name("web_search")

    cfg.write_text(json.dumps(written), encoding="utf-8")

    assert loop.tools.offers_by_name("web_search"), f"{layout}: the added key never reached the tool"
    assert loop.tools.get("web_search").api_key == "sk-added-later"


def test_a_key_cleared_after_start_withdraws_web_search(workspace, tmp_path: Path, monkeypatch) -> None:
    """The other direction, and the reason an empty slot is an answer rather
    than a miss: a revoked key must not fall through to the boot value the
    process started with."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"web": {"providers": {"serper": {"apiKey": "sk-boot"}}}}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    loop = _loop(workspace, web_provider_keys={"serper": "sk-boot"})
    assert loop.tools.offers_by_name("web_search")

    cfg.write_text(json.dumps({"tools": {"web": {"providers": {"serper": {"apiKey": ""}}}}}), encoding="utf-8")

    assert not loop.tools.offers_by_name("web_search")
    assert loop.tools.has("web_search"), "withheld, not unregistered"


def test_the_subagent_loop_applies_the_same_rule(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # A sub-agent reaching for a search it cannot run reports the failure to its
    # caller, and that text lands in the parent turn.
    #
    # The backend builds its registry inside `run` and keeps no reference, so the
    # names are observed as they are registered. The collector opens before the
    # backend is constructed and drops nothing on the floor: were a registration
    # ever to happen outside a window, it would land in the previous run's list
    # and be caught, rather than vanishing and leaving an assertion that passes
    # over an empty list.
    registered: list[list[str]] = []
    real = ToolRegistry.register

    def _spy(self, tool):  # noqa: ANN001, ANN202
        real(self, tool)
        assert registered, f"{tool.name} was registered outside a collection window"
        registered[-1].append(tool.name)

    monkeypatch.setattr(ToolRegistry, "register", _spy)

    async def _names(**kw) -> list[str]:
        registered.append([])
        backend = RavenLoopBackend(provider=_StubProvider(), model="stub", agent_home=workspace / "home", **kw)
        await backend.run("task", task_id="t1", workspace=workspace, executor=None)
        return registered[-1]

    import asyncio

    without = asyncio.run(_names())
    with_key = asyncio.run(_names(search_api_key="sk-serper"))

    # Baselines first: an empty list would satisfy the "not in" assertion below
    # without proving anything about the gate.
    assert "read_file" in without and "web_fetch" in without, without
    assert "read_file" in with_key and "web_fetch" in with_key, with_key
    assert "web_search" not in without
    assert "web_search" in with_key


def test_the_selected_vendors_key_is_what_offers_web_search(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate asks the tool built for the selected vendor. A Serper key does
    not offer a Tavily search, and Tavily's own env var does.

    Offered rather than registered: the tool is always registered and withheld
    from the advertised array while no key resolves, so the assertions here are
    about the offer. A gate that named ``SERPER_API_KEY`` itself instead of
    asking the tool passed every case but the first.
    """
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    serper_keyed = _loop(workspace, web_search_provider="tavily", search_api_key="sk-serper")
    assert not serper_keyed.tools.offers_by_name("web_search")
    assert serper_keyed.tools.has("web_search"), "withheld, not unregistered"

    with_key = _loop(workspace, web_search_provider="tavily", web_provider_keys={"tavily": "tv"})
    assert with_key.tools.offers_by_name("web_search")
    assert with_key.tools.get("web_search").provider == "tavily"

    monkeypatch.setenv("TAVILY_API_KEY", "tv-env")
    assert _loop(workspace, web_search_provider="tavily").tools.offers_by_name("web_search")


def test_a_keyed_reader_without_a_key_registers_jina_instead(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """web_fetch stays unconditional: the reader that cannot run is replaced by
    the one that needs no key, out loud, rather than offered to fail."""
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    loop = _loop(workspace, web_fetch_provider="firecrawl")
    assert loop.tools.get("web_fetch").provider == "jina"

    keyed = _loop(workspace, web_fetch_provider="firecrawl", web_provider_keys={"firecrawl": "fc"})
    assert keyed.tools.get("web_fetch").provider == "firecrawl"
    assert keyed.tools.get("web_fetch").api_key == "fc"


@pytest.mark.asyncio
async def test_the_unconfigured_error_names_the_config_actually_in_force(tmp_path: Path) -> None:
    """Reachable only if the key disappears after registration, but the message
    used to hard-code ``~/.raven/config.json`` and send anyone running with
    ``--config`` to edit a file the process never reads."""
    from raven.config.loader import get_config_path, set_config_path

    before = get_config_path()
    chosen = tmp_path / "elsewhere.json"
    try:
        set_config_path(chosen)
        out = await WebSearchTool().execute(query="anything")
    finally:
        set_config_path(before)

    assert str(chosen) in out
    assert "~/.raven/config.json" not in out


class _PluginSearch(Tool):
    """A plugin's own web_search, registered last so it shadows the built-in."""

    name = "web_search"
    description = "plugin search with its own credential story"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "plugin ran"


def test_a_plugin_shadowing_web_search_is_not_gated_by_the_builtin_config(workspace) -> None:
    # Plugin tools register last precisely so one can replace a built-in by
    # name; the unconfigured gate reads the BUILT-IN's config and must judge
    # only the built-in instance, or it hides the plugin behind a section that
    # says nothing about it.
    plugin = _PluginSearch()
    loop = _loop(workspace, plugin_tools=[plugin])

    assert loop.tools.get("web_search") is plugin
    assert loop.tools.offers_by_name("web_search"), "the built-in's empty section must not gate the plugin"


def test_a_plugin_subclass_of_web_search_is_not_gated_either(workspace) -> None:
    # The ownership check is identity, not type: an isinstance gate read a
    # plugin SUBCLASS as the built-in itself and withheld it on the built-in's
    # empty section, even though the subclass carries its own credential story.
    class _PluginSubclassSearch(WebSearchTool):
        def __init__(self) -> None:
            super().__init__(api_key="sk-plugin-own")

    plugin = _PluginSubclassSearch()
    loop = _loop(workspace, plugin_tools=[plugin])

    assert loop.tools.get("web_search") is plugin
    assert loop.tools.offers_by_name("web_search")


def test_a_subagent_exec_tool_reads_the_live_deny_source(workspace, tmp_path: Path, monkeypatch) -> None:
    """A tightened permission gates a delegated shell the same call it gates a
    direct one: the sub-agent's ExecTool reads the same live deny source the
    main loop's does, not a construction-time snapshot."""
    import asyncio

    from raven.agent.tools.shell import ExecTool

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"exec": {"extraDenyPatterns": []}}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)

    captured: list[ExecTool] = []
    real = ToolRegistry.register

    def _spy(self, tool, **kw):  # noqa: ANN001, ANN202
        real(self, tool, **kw)
        if isinstance(tool, ExecTool):
            captured.append(tool)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    backend = RavenLoopBackend(provider=_StubProvider(), model="stub", agent_home=workspace / "home")
    asyncio.run(backend.run("task", task_id="t1", workspace=workspace, executor=None))
    assert captured, "the backend registered no ExecTool"
    tool = captured[-1]

    cfg.write_text(json.dumps({"tools": {"exec": {"extraDenyPatterns": ["\\bosascript\\b"]}}}), encoding="utf-8")

    result = asyncio.run(tool.execute("osascript -e beep"))
    assert "blocked" in result.model_text
