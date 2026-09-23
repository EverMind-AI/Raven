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
from raven.agent.tools.web import ImageSearchTool, WebSearchTool
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


class _RefusingThenServing:
    """Stands in for ``httpx.AsyncClient``: refuses the boot key, serves the new one."""

    def __init__(self) -> None:
        self.keys_seen: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, **kwargs):
        import httpx

        key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        self.keys_seen.append(key)
        request = httpx.Request("POST", url)
        if key == "sk-boot":
            return httpx.Response(402, json={}, request=request)
        return httpx.Response(200, json={"results": [{"url": url, "raw_content": "PAGE"}]}, request=request)


@pytest.mark.asyncio
async def test_a_reader_key_set_after_a_refusal_reaches_the_next_call(workspace, tmp_path: Path, monkeypatch) -> None:
    """The refusal tells the user to set a working key at the vendor's slot. The
    tool used to hold the key it was built with, so following that instruction
    did nothing until a restart; the slot is now what the next call reads."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"web": {"providers": {"tavily": {"apiKey": "sk-boot"}}}}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    monkeypatch.setattr("raven.agent.tools.web.validate_url_target", lambda url: (True, ""))
    client = _RefusingThenServing()
    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", client)
    loop = _loop(workspace, web_fetch_provider="tavily", web_provider_keys={"tavily": "sk-boot"})
    tool = loop.tools.get("web_fetch")
    assert tool.provider == "tavily"

    refused = json.loads(await tool.execute("https://a.example"))
    paused = json.loads(await tool.execute("https://b.example"))
    assert refused["error"] == "Tavily refused the key (HTTP 402)" and paused["paused"] is True
    assert client.keys_seen == ["sk-boot"], "the paused call was not sent"

    cfg.write_text(
        json.dumps({"tools": {"web": {"providers": {"tavily": {"apiKey": "sk-rotated"}}}}}), encoding="utf-8"
    )

    served = json.loads(await tool.execute("https://c.example"))
    assert served["text"] == "PAGE"
    assert client.keys_seen == ["sk-boot", "sk-rotated"]
    assert tool.api_key == "sk-rotated"


@pytest.mark.asyncio
async def test_a_reader_whose_key_is_cleared_in_the_file_reads_through_jina(
    workspace, tmp_path: Path, monkeypatch
) -> None:
    """Clearing ``tools.web.providers.<vendor>.apiKey`` is the edit the refusal
    text points the user at. The reader registered on that vendor then runs on
    Jina, keyless, from the next call, rather than sending an empty credential."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"web": {"providers": {"tavily": {"apiKey": "sk-boot"}}}}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    monkeypatch.setattr("raven.agent.tools.web.validate_url_target", lambda url: (True, ""))
    sent: list[tuple[str, dict]] = []

    class _Reader:
        def __call__(self, *a, **k):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, **kwargs):
            import httpx

            sent.append((url, kwargs.get("headers") or {}))
            payload = {"results": [{"url": url, "raw_content": "TAVILY"}]}
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

        async def get(self, url, **kwargs):
            import httpx

            sent.append((url, kwargs.get("headers") or {}))
            return httpx.Response(200, text="JINA", request=httpx.Request("GET", url))

    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", _Reader())
    loop = _loop(workspace, web_fetch_provider="tavily", web_provider_keys={"tavily": "sk-boot"})
    tool = loop.tools.get("web_fetch")

    first = json.loads(await tool.execute("https://a.example"))
    cfg.write_text(json.dumps({"tools": {"web": {"providers": {"tavily": {"apiKey": ""}}}}}), encoding="utf-8")
    second = json.loads(await tool.execute("https://b.example"))

    assert first["extractor"] == "tavily-extract" and sent[0][1]["Authorization"] == "Bearer sk-boot"
    assert second["extractor"] == "jina-reader" and second["text"] == "JINA"
    assert sent[1][0].startswith("https://r.jina.ai/") and "Authorization" not in sent[1][1]
    assert tool.provider == "jina"


@pytest.mark.asyncio
async def test_the_subagent_lanes_web_tools_read_their_keys_live_too(tmp_path: Path, monkeypatch) -> None:
    """All three tools carry the advice to set a key at the config slot, so all
    three read it there; a boot-snapshot key on two of them made that advice
    false for one sub-agent run."""
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
    from raven.agent.tools.registry import ToolRegistry

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)
    registered: list = []
    real = ToolRegistry.register

    def _spy(self, tool):
        real(self, tool)
        registered.append(tool)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    backend = RavenLoopBackend(
        provider=_StubProvider(),
        model="stub",
        agent_home=tmp_path,
        web_search_provider="tavily",
        web_fetch_provider="tavily",
        web_provider_keys={"tavily": "sk-boot"},
        image_search=True,
    )
    await backend.run("task", task_id="t1", workspace=tmp_path, executor=None)
    tools = {t.name: t for t in registered if t.name in ("web_search", "image_search", "web_fetch")}
    assert sorted(tools) == ["image_search", "web_fetch", "web_search"]
    assert {t.provider for t in tools.values()} == {"tavily"}
    assert {t.api_key for t in tools.values()} == {"sk-boot"}

    cfg.write_text(
        json.dumps({"tools": {"web": {"providers": {"tavily": {"apiKey": "sk-rotated"}}}}}), encoding="utf-8"
    )

    assert {name: t.api_key for name, t in tools.items()} == {name: "sk-rotated" for name in tools}


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
    direct one: the sub-agent's gate reads the same live deny source the main
    loop's does, not a construction-time snapshot."""
    import asyncio

    from raven.agent.tools.shell import ExecTool
    from raven.permissions.turn import start_permission_turn

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tools": {"exec": {"extraDenyPatterns": []}}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)

    captured: list[ToolRegistry] = []
    real = ToolRegistry.register

    def _spy(self, tool, **kw):  # noqa: ANN001, ANN202
        real(self, tool, **kw)
        if isinstance(tool, ExecTool):
            captured.append(self)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    backend = RavenLoopBackend(provider=_StubProvider(), model="stub", agent_home=workspace / "home")
    asyncio.run(backend.run("task", task_id="t1", workspace=workspace, executor=None))
    assert captured, "the backend registered no ExecTool"
    registry = captured[-1]

    cfg.write_text(json.dumps({"tools": {"exec": {"extraDenyPatterns": ["\\bosascript\\b"]}}}), encoding="utf-8")

    start_permission_turn(None, conversation_id="sub", turn_id="t1")
    result = asyncio.run(registry.execute("exec", {"command": "osascript -e beep"}))
    assert "blocked" in str(result)


def test_a_subagent_gate_asks_through_the_parent_turns_responder(workspace, tmp_path: Path, monkeypatch) -> None:
    """A sub-agent's gate reads the turn's mode instead of pinning full, and
    reaches the responder the parent turn bound (a ContextVar its task
    inherits): a delegated exec in ask mode asks the same human a direct one
    would, rather than running in silence or being refused outright."""
    import asyncio

    from raven.agent.tools.shell import ExecTool
    from raven.contracts.permissions import ApprovalChoice, ApprovalOutcome
    from raven.permissions.turn import start_permission_turn

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"permissions": {"mode": "ask"}}), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg)

    captured: list[ToolRegistry] = []
    real = ToolRegistry.register

    def _spy(self, tool, **kw):  # noqa: ANN001, ANN202
        real(self, tool, **kw)
        if isinstance(tool, ExecTool):
            captured.append(self)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    backend = RavenLoopBackend(provider=_StubProvider(), model="stub", agent_home=workspace / "home")
    asyncio.run(backend.run("task", task_id="t1", workspace=workspace, executor=None))
    assert captured, "the backend registered no ExecTool"
    registry = captured[-1]

    class _Click:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def await_approval(self, **request):  # noqa: ANN003, ANN202
            self.seen.append(request["command"])
            return ApprovalOutcome(choice=ApprovalChoice.DENY)

    click = _Click()
    start_permission_turn(click, conversation_id="sub", turn_id="t1")
    # A write, not a read: `echo` would run without asking under the read-only
    # default, and this test is about the ask reaching the parent's responder.
    result = asyncio.run(registry.execute("exec", {"command": "touch delegated"}))
    assert click.seen == ["touch delegated"]
    assert "denied" in str(result).lower()


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_design_image_offer_and_execution_follow_host_selection(tmp_path, initially_enabled):
    from raven.config.live import LiveConfig
    from raven.config.schema import MediaGenConfig

    host = tmp_path / "host.json"
    selection = {"apiKey": "host-key", "model": "openai/gpt-image-2"}

    def write(enabled):
        host.write_text(json.dumps({"tools": {"media": {"image": selection if enabled else {}}}}))

    write(initially_enabled)
    startup = {**(selection if initially_enabled else {}), "selectionConfig": str(host)}
    loop = _loop(tmp_path, media_config=MediaGenConfig.model_validate({"image": startup}))
    loop._live_config = LiveConfig(tmp_path / "absent-render.json")
    tool = loop.tools.get("image_generate")
    for enabled in [initially_enabled, not initially_enabled, initially_enabled]:
        write(enabled)
        assert ("image_generate" not in loop._unconfigured_tool_names()) is enabled
        assert tool._config.api_key == ("host-key" if enabled else "")
        assert tool._config.model == ("openai/gpt-image-2" if enabled else "")
    host.write_text(json.dumps({"tools": {}}))
    assert "image_generate" in loop._unconfigured_tool_names()
    assert not tool._config.api_key


def test_image_search_is_withheld_without_a_serper_key_and_offered_with_one(workspace) -> None:
    """The Design lane was told to call Serper's image endpoint from `exec` with a key in
    a file nothing named; a live run found no key and generated every picture. A tool,
    gated like web_search, is what the lane -- and the host -- reach for instead."""
    unasked = _loop(workspace, search_api_key="sk-serper")
    assert not unasked.tools.has("image_search"), (
        "a loop that did not ask for pictures has no picture tool, keyed or not"
    )

    bare = _loop(workspace, image_search=True)
    assert bare.tools.has("image_search") and not bare.tools.offers_by_name("image_search")

    keyed = _loop(workspace, search_api_key="sk-serper", image_search=True)
    assert keyed.tools.offers_by_name("image_search")


def test_image_search_follows_a_selected_vendor_that_has_an_image_surface(workspace) -> None:
    loop = _loop(workspace, web_search_provider="tavily", web_provider_keys={"tavily": "tv-key"}, image_search=True)
    assert loop.tools.offers_by_name("web_search") and loop.tools.offers_by_name("image_search")
    assert loop.tools.get("image_search").provider == "tavily"


def test_image_search_falls_back_to_serper_when_the_selected_vendor_searches_pages_only(workspace) -> None:
    on_exa = _loop(workspace, web_search_provider="exa", web_provider_keys={"exa": "exa-key"}, image_search=True)
    assert on_exa.tools.offers_by_name("web_search"), "Exa searches pages"
    assert not on_exa.tools.offers_by_name("image_search"), "Exa has no image surface and Serper holds no key"
    assert on_exa.tools.get("image_search").provider == "serper"

    with_serper = _loop(
        workspace, web_search_provider="exa", web_provider_keys={"exa": "exa-key", "serper": "sk"}, image_search=True
    )
    assert with_serper.tools.offers_by_name("image_search"), "Serper's key opens pictures beside Exa's pages"

    unkeyed_tavily = _loop(workspace, web_search_provider="tavily", search_api_key="sk-serper", image_search=True)
    assert unkeyed_tavily.tools.get("image_search").provider == "serper", "a selected vendor without a key gives way"
    assert unkeyed_tavily.tools.offers_by_name("image_search")


class _ImageResponse:
    def __init__(self, hits):
        self._hits = hits

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"images": self._hits}


class _ImageClient:
    asked: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url, json=None, headers=None, timeout=None):
        type(self).asked.append(json["q"])
        return _ImageResponse(
            [
                {
                    "title": f"{json['q']} wide",
                    "imageUrl": f"https://pics.example/{json['q']}.png",
                    "imageWidth": 1600,
                    "imageHeight": 900,
                    "domain": "example.com",
                    "link": f"https://example.com/{json['q']}",
                },
                {
                    "title": "thumbnail",
                    "imageUrl": "https://pics.example/small.png",
                    "imageWidth": 200,
                    "imageHeight": 150,
                    "domain": "example.com",
                },
            ]
        )


@pytest.mark.asyncio
async def test_image_search_returns_url_size_and_source_and_drops_what_a_screen_cannot_use(monkeypatch) -> None:
    monkeypatch.setattr("raven.agent.tools.web.httpx.AsyncClient", _ImageClient)
    _ImageClient.asked = []
    tool = ImageSearchTool(api_key="k")

    said = await tool.execute(queries=["gugong", "tiantan"])

    assert _ImageClient.asked == ["gugong", "tiantan"]
    assert "Image results for: gugong" in said and "Image results for: tiantan" in said
    assert "https://pics.example/gugong.png" in said and "1600x900px - example.com" in said
    assert "from: https://example.com/gugong" in said
    assert "small.png" not in said, "200px wide is a thumbnail, not a picture"
    assert "Error" in await tool.execute(), "neither form is a search"
    assert "API key not configured" in await ImageSearchTool(api_key=None).execute(query="x")


@pytest.mark.parametrize(
    ("vendor", "payload"),
    [
        (
            "serpapi",
            {
                "images_results": [
                    {
                        "title": "gate",
                        "original": "https://p.example/g.jpg",
                        "original_width": 1600,
                        "original_height": 900,
                        "source": "example.com",
                        "link": "https://example.com/gate",
                    }
                ]
            },
        ),
        (
            "brave",
            {
                "results": [
                    {
                        "title": "gate",
                        "url": "https://example.com/gate",
                        "source": "example.com",
                        "properties": {"url": "https://p.example/g.jpg", "width": 1600, "height": 900},
                    }
                ]
            },
        ),
        (
            "firecrawl",
            {
                "data": {
                    "images": [
                        {
                            "title": "gate",
                            "imageUrl": "https://p.example/g.jpg",
                            "imageWidth": 1600,
                            "imageHeight": 900,
                            "url": "https://example.com/gate",
                            "position": 1,
                        }
                    ]
                }
            },
        ),
    ],
)
def test_every_vendor_with_an_image_surface_is_read_into_the_same_hit(vendor, payload) -> None:
    hit = ImageSearchTool(api_key="k", provider=vendor).normalise_hits(payload)[0]
    assert (hit.title, hit.image_url, hit.width, hit.height) == ("gate", "https://p.example/g.jpg", 1600, 900)
    assert hit.source == "example.com" and hit.page == "https://example.com/gate"


def test_tavily_hits_come_without_a_size_and_are_offered_as_such() -> None:
    tool = ImageSearchTool(api_key="k", provider="tavily")
    hits = tool.normalise_hits(
        {
            "images": [
                {"url": "https://cdn.example/a.jpg", "description": "the gate at dusk"},
                "https://cdn.example/b.jpg",
            ]
        }
    )
    assert [h.image_url for h in hits] == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
    assert hits[0].title == "the gate at dusk" and hits[0].width is None and hits[0].source == "cdn.example"


def test_a_vendor_without_an_image_surface_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        ImageSearchTool(api_key="k", provider="exa")
