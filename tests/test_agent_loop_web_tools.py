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
    loop = _loop(workspace, brave_api_key="sk-serper")

    assert loop.tools.has("web_search")


def test_the_env_var_alone_registers_web_search(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # The tool resolves its key at call time from the config value *or*
    # SERPER_API_KEY, so a gate that reads only the config would withdraw the
    # tool from a deploy that exports the variable and configures nothing.
    monkeypatch.setenv("SERPER_API_KEY", "sk-from-env")

    loop = _loop(workspace)

    assert loop.tools.has("web_search")


def test_the_subagent_surface_applies_the_same_rule(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # A sub-agent reaching for a search it cannot run reports the failure to its
    # caller, and that text lands in the parent turn.
    #
    # The manager builds its registry inside the run and keeps no reference, so
    # the names are observed as they are registered. The collector opens before
    # the manager is constructed and drops nothing on the floor: were a
    # registration ever to happen outside a window, it would land in the previous
    # run's list and be caught, rather than vanishing and leaving an assertion
    # that passes over an empty list.
    registered: list[list[str]] = []
    real = ToolRegistry.register

    def _spy(self, tool):  # noqa: ANN001, ANN202
        real(self, tool)
        assert registered, f"{tool.name} was registered outside a collection window"
        registered[-1].append(tool.name)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    # The run announces its result through the spine, which is not wired here and
    # is not what this is about.
    monkeypatch.setattr(SubagentManager, "_announce_result", _noop)

    async def _names(**kw) -> list[str]:
        registered.append([])
        manager = SubagentManager(provider=_StubProvider(), workspace=workspace, model="stub", **kw)
        await manager._run_subagent_inner("t1", "task", "label", {}, None, manager.provider, manager.model)
        return registered[-1]

    import asyncio

    without = asyncio.run(_names())
    with_key = asyncio.run(_names(brave_api_key="sk-serper"))

    # Baselines first: an empty list would satisfy the "not in" assertion below
    # without proving anything about the gate.
    assert "read_file" in without and "web_fetch" in without, without
    assert "read_file" in with_key and "web_fetch" in with_key, with_key
    assert "web_search" not in without
    assert "web_search" in with_key


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
