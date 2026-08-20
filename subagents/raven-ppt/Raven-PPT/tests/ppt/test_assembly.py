"""What gets registered, and the failure the predecessor could not see.

Its assembly function carried ten branches on one boolean, two of which
contradicted each other: a sub-route was registered under
`expert_outline and not free_composition` while `expert_outline` already implied
`free_composition`. Fifteen hundred lines of tooling were unreachable under every
configuration for months, and a test had frozen that as the expected behaviour.

The lesson is not "check that predicate". It is that assembly must verify its own
result against the route's declaration instead of trusting the arithmetic that
produced it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from raven.ppt.profiles import registry
from raven.ppt.tools.assembly import build_ppt_tools

pytest.importorskip("pptx")


def test_the_default_route_registers_its_tools(tmp_path: Path) -> None:
    names = {tool.name for tool in build_ppt_tools(tmp_path)}
    assert "ppt_build" in names


def test_every_registered_tool_is_one_the_route_asked_for(tmp_path: Path) -> None:
    profile = registry.get(registry.DEFAULT)
    names = {tool.name for tool in build_ppt_tools(tmp_path)}
    assert names <= set(profile.tools), names - set(profile.tools)


def test_a_route_missing_a_tool_says_so_at_startup(tmp_path: Path, caplog) -> None:
    """Rather than at the point a model calls a tool nobody registered."""
    with caplog.at_level(logging.WARNING):
        build_ppt_tools(tmp_path)
    missing = [r for r in caplog.records if "were not registered" in r.getMessage()]
    if missing:
        # The message must name what is absent; a warning that does not is noise.
        assert "ppt_ingest" in missing[0].getMessage() or "ppt_figure_inspect" in missing[0].getMessage()


def test_an_unknown_route_falls_back_and_says_which(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        tools = build_ppt_tools(tmp_path, profile="no_such_route")
    assert tools
    assert any("falling back" in r.getMessage() for r in caplog.records)


def test_a_declared_route_with_no_backend_yet_registers_nothing(tmp_path: Path, caplog) -> None:
    """Saying so beats registering a route that fails at the first call."""
    with caplog.at_level(logging.WARNING):
        assert build_ppt_tools(tmp_path, profile="image_text") == []
    assert any("not implemented yet" in r.getMessage() for r in caplog.records)


def test_without_a_provider_there_is_no_design_pass_but_the_gates_remain(tmp_path: Path) -> None:
    """What is lost is the second pair of eyes, not the checks."""
    tool = next(t for t in build_ppt_tools(tmp_path) if t.name == "ppt_build")
    assert tool.stage.design_pass is None
    assert tool.stage.measure is not None


class _Provider:
    async def chat_stream(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError


def test_with_a_provider_the_design_pass_shares_the_stage_s_measurer(tmp_path: Path) -> None:
    """Its own would let a page pass one check and fail the one that matters."""
    tools = build_ppt_tools(tmp_path, provider=_Provider(), design_pass=True)
    tool = next(t for t in tools if t.name == "ppt_build")
    assert tool.stage.design_pass is not None
    assert tool.stage.design_pass.measure is tool.stage.measure


def test_a_provider_alone_does_not_switch_the_design_pass_on(tmp_path: Path) -> None:
    """It is off unless the install asks for it -- see PptDesignerConfig for the
    measurements that made it opt-in: a round that deleted 224 characters of a
    deck's copy, and an accent rail added to eight pages that the deck's own gate
    reads as a colour band. A provider being available says nothing about that.
    """
    tool = next(t for t in build_ppt_tools(tmp_path, provider=_Provider()) if t.name == "ppt_build")
    assert tool.stage.design_pass is None


def test_the_schema_never_offers_a_physical_quantity(tmp_path: Path) -> None:
    banned = ("emu", "inch", "inches", "px", "pt", "font", "size", "x0", "y0", "width", "height")
    for tool in build_ppt_tools(tmp_path):
        for name in tool.parameters.get("properties") or {}:
            assert name.lower() not in banned, f"{tool.name}.{name}"
