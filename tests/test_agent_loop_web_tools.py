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

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent.manager import SubagentManager
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.web import WebSearchTool
from raven.config.schema import WebSearchConfig
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
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _no_ambient_serper_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool falls back to ``SERPER_API_KEY``, so a developer who exports one
    would otherwise see these tests pass for the wrong reason."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)


def _loop(workspace: Path, **kw) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=workspace, model="stub", **kw)


async def _noop(*_a, **_kw) -> None:
    return None


def test_web_search_is_withheld_without_a_key(workspace) -> None:
    loop = _loop(workspace)

    assert not loop.tools.has("web_search"), (
        "offering a search that cannot run makes the model relay the tool's setup error to the user"
    )
    assert loop.tools.has("web_fetch"), "web_fetch needs no key and must stay unconditional"


def test_a_configured_key_registers_web_search(workspace) -> None:
    loop = _loop(workspace, web_search_config=WebSearchConfig(api_key="sk-serper"))

    assert loop.tools.has("web_search")


def test_the_configured_result_count_reaches_the_tool(workspace) -> None:
    """``tools.web.search.maxResults`` was declared and never wired.

    Both registration sites passed the key alone, so the tool fell back to its
    own default and a deployer who set the field got no effect and no warning.
    Passing the section rather than one field out of it is what closes that, and
    this is the assertion that keeps it closed.
    """
    loop = _loop(workspace, web_search_config=WebSearchConfig(api_key="sk-serper", max_results=9))

    assert loop.tools.get("web_search").max_results == 9


def test_the_env_var_alone_registers_web_search(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool resolves its key at call time from the config value *or*
    # SERPER_API_KEY, so a gate that reads only the config would withdraw the
    # tool from a deploy that exports the variable and configures nothing.
    monkeypatch.setenv("SERPER_API_KEY", "sk-from-env")

    loop = _loop(workspace)

    assert loop.tools.has("web_search")


@pytest.fixture
def subagent_run(workspace, monkeypatch: pytest.MonkeyPatch):
    """Run a sub-agent and hand back the tools it registered.

    The manager builds its registry inside the run and keeps no reference, so
    the tools are observed as they are registered. The collector opens before
    the manager is constructed and drops nothing on the floor: were a
    registration ever to happen outside a window, it would land in the previous
    run's list and be caught, rather than vanishing and leaving an assertion
    that passes over an empty list.
    """
    import asyncio

    runs: list[list] = []
    real = ToolRegistry.register

    def _spy(self, tool):  # noqa: ANN001, ANN202
        real(self, tool)
        assert runs, f"{tool.name} was registered outside a collection window"
        runs[-1].append(tool)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    # The run announces its result through the spine, which is not wired here
    # and is not what these are about.
    monkeypatch.setattr(SubagentManager, "_announce_result", _noop)

    def run(**kw):
        runs.append([])
        manager = SubagentManager(provider=_StubProvider(), workspace=workspace, model="stub", **kw)
        asyncio.run(manager._run_subagent_inner("t1", "task", "label", {}, None, manager.provider, manager.model))
        return runs[-1]

    return run


def test_the_subagent_surface_applies_the_same_rule(subagent_run) -> None:
    # A sub-agent reaching for a search it cannot run reports the failure to its
    # caller, and that text lands in the parent turn.
    without = [t.name for t in subagent_run()]
    with_key = [t.name for t in subagent_run(web_search_config=WebSearchConfig(api_key="sk-serper"))]

    # Baselines first: an empty list would satisfy the "not in" assertion below
    # without proving anything about the gate.
    assert "read_file" in without and "web_fetch" in without, without
    assert "read_file" in with_key and "web_fetch" in with_key, with_key
    assert "web_search" not in without
    assert "web_search" in with_key


def test_the_subagent_surface_gets_the_configured_result_count_too(subagent_run) -> None:
    """The second registration site had the same unwired field, and a fix
    applied to one of two call sites is the shape this whole area keeps taking."""
    tools = subagent_run(web_search_config=WebSearchConfig(api_key="sk-serper", max_results=7))

    web_search = next(t for t in tools if t.name == "web_search")
    assert web_search.max_results == 7


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
