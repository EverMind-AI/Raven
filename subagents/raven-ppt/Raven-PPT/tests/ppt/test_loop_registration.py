"""How the deck tools reach an agent, and what stays untouched.

The predecessor did not add PPT to the trunk; it converted the trunk. The agent's
identity text, its bootstrap file loader and its active-skill allowlist were all
replaced with PPT-only versions, which is why that fork could only make decks.

Deck authoring is this build's default form, so the tools are on unless a config
turns them off -- an install where deck making is opt-in is one where the first
request goes to the wrong tools. What this file protects is the other half, which
the default does not change: the capability is still self-contained. Nothing
PPT-shaped is hard-coded into the prompt renderer or the loader, the deck identity
travels as workspace assets and an always-on skill, and turning the flag off gives
back the general agent whole.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.config.schema import PptToolConfig, ToolsConfig

pytest.importorskip("pptx")


class _Loop:
    """The one method under test, with the collaborators it actually touches."""

    def __init__(self, workspace: Path, config: Any) -> None:
        from raven.agent.loop.main import AgentLoop
        from raven.agent.tools.registry import ToolRegistry

        self.workspace = workspace
        self._ppt_config = config
        self.provider = None
        self.web_proxy = None
        self.tools = ToolRegistry()
        self._register = AgentLoop._register_ppt_tools.__get__(self)

    def run(self) -> set[str]:
        self._register()
        return {name for name in self.tools._tools}


def test_on_by_default(tmp_path: Path) -> None:
    assert ToolsConfig().ppt.enabled is True
    assert _Loop(tmp_path, ToolsConfig().ppt).run()


def test_turning_it_off_gives_back_the_general_agent(tmp_path: Path) -> None:
    assert _Loop(tmp_path, PptToolConfig(enabled=False)).run() == set()


def test_the_prompt_renderer_knows_nothing_about_decks(tmp_path: Path) -> None:
    """The predecessor's mistake, and the reason the default flipping is safe.

    The deck identity travels as workspace assets (`raven/templates/`) and an
    always-on skill, so the renderer stays the general one and a build with the
    flag off is not a build with deck prose in its prompt.
    """
    from raven.context_engine.segments import render

    text = render.identity_text(tmp_path).lower()
    for word in ("deck", "slide", "pptx", "ppt_build"):
        assert word not in text, f"{word!r} reached the shared identity text"


def test_absent_config_registers_nothing(tmp_path: Path) -> None:
    assert _Loop(tmp_path, None).run() == set()


def test_enabling_it_registers_the_route_s_tools(tmp_path: Path) -> None:
    names = _Loop(tmp_path, PptToolConfig(enabled=True)).run()
    assert "ppt_build" in names
    assert "ppt_ingest" in names


def test_a_route_with_no_backend_yet_registers_nothing(tmp_path: Path) -> None:
    names = _Loop(tmp_path, PptToolConfig(enabled=True, profile="image_text")).run()
    assert names == set()


def test_the_config_reaches_the_loop_from_every_cli_surface() -> None:
    """Three surfaces construct the loop; a route configured on one and not the
    others is the kind of gap nobody notices until a deck run does nothing."""
    for name in ("agent_commands", "gateway_commands", "tui_commands"):
        source = (Path("raven/cli") / f"{name}.py").read_text(encoding="utf-8")
        assert "ppt_config=config.tools.ppt," in source, name
