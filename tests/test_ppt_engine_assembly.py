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

from raven_ppt.profiles import registry
from raven_ppt.tools.assembly import build_ppt_tools

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


class _Provider:
    async def chat_stream(self, *a, **k):  # pragma: no cover - never called here
        raise AssertionError


def test_the_gates_stand_with_or_without_a_second_model(tmp_path: Path) -> None:
    """A provider used to decide whether a whole stage existed. It buys per-page calls
    inside `ppt_prepare` and `ppt_outline` now, and nothing about what a build measures:
    the same measurer is wired either way."""
    for tools in (build_ppt_tools(tmp_path), build_ppt_tools(tmp_path, provider=_Provider())):
        tool = next(t for t in tools if t.name == "ppt_build")
        assert tool.stage.measure is not None


# The vocabulary a model must never be handed: a type size, a font face, or a
# physical coordinate. Matched against the parts of a field name, so `font_size` and
# `slide_width` are caught the way `font` and `width` are.
PHYSICAL_QUANTITIES = (
    "emu",
    "inch",
    "inches",
    "px",
    "pt",
    "font",
    "typeface",
    "face",
    "size",
    "width",
    "height",
    "x0",
    "y0",
    "x1",
    "y1",
    "left",
    "top",
)

# Normalised composition coordinates are the one documented exception (CLAUDE.md's
# hard invariant 1): a free-composition region is a 0..1000 grid and a vector canvas
# is 160x90, and neither is a physical quantity -- the engine maps them. Listed as
# whole field names rather than allowed by a rule, so the exception cannot widen on
# its own, and empty until a route actually declares one.
NORMALISED_FIELDS: frozenset[str] = frozenset()


def _named_fields(schema: object, path: str = "") -> list[str]:
    """Every field name anywhere in a JSON schema, with the route to it.

    The check used to read `properties` at the top level only, which is the one place
    a physical quantity was never going to appear: an outline's pages, a page's table
    plan and a chart's series are all nested objects, and `ppt_outline` alone puts
    three levels of them behind one top-level `pages`. Everything a schema can nest a
    named field inside is followed -- `items`, the combinators, `$defs` and
    `additionalProperties` when it is a schema rather than `False`.
    """
    found: list[str] = []
    if isinstance(schema, list):
        for index, entry in enumerate(schema):
            found.extend(_named_fields(entry, f"{path}[{index}]"))
        return found
    if not isinstance(schema, dict):
        return found
    for key, value in schema.items():
        if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(value, dict):
            for name, subschema in value.items():
                found.append(f"{path}.{name}" if path else name)
                found.extend(_named_fields(subschema, f"{path}.{name}" if path else name))
        elif key in ("items", "prefixItems", "anyOf", "oneOf", "allOf", "not", "contains", "additionalProperties"):
            found.extend(_named_fields(value, path))
    return found


def test_the_schema_never_offers_a_physical_quantity(tmp_path: Path) -> None:
    """The hard invariant, checked at every depth a schema can hide a field at.

    Two holes, and each made the check weaker than the sentence it was written for.
    It compared the whole field name for equality against the banned list, so
    `font_size`, `slide_width` and `body_font` all passed a test whose point is that
    none of them may exist. And it read only the top-level `properties`, so every
    field inside `ppt_outline`'s pages -- the part of the schema a model writes most
    of -- was never looked at at all. Nothing violates it today, which is why the
    weakness could sit there: a test that passes for the wrong reason looks exactly
    like a test that passes.
    """
    import re

    offences = []
    for tool in build_ppt_tools(tmp_path):
        for field in _named_fields(tool.parameters):
            name = field.rsplit(".", 1)[-1]
            if name in NORMALISED_FIELDS:
                continue
            parts = {part.lower() for part in re.split(r"_|(?<=[a-z])(?=[A-Z])", name) if part}
            caught = parts & set(PHYSICAL_QUANTITIES)
            if caught:
                offences.append(f"{tool.name}.{field} names {sorted(caught)}")
    assert not offences, "the schema hands a model a physical quantity:\n  " + "\n  ".join(offences)


def test_the_scan_reaches_the_fields_the_flat_one_could_not_see(tmp_path: Path) -> None:
    """The guard on the guard: a recursive check that recursed into nothing would pass.

    `ppt_outline` nests every page's fields under an array's `items`, so
    `pages.claim` is only reachable by recursing through that array -- the shape the
    old flat check was blind to, and the cheapest proof the new one is not. The
    planted schema below carries the same nesting with a banned word at the bottom.
    """
    tools = {tool.name: tool for tool in build_ppt_tools(tmp_path)}
    outline = tools.get("ppt_outline")
    if outline is None:
        pytest.skip("the outline tool is not registered on the default route")
    fields = _named_fields(outline.parameters)
    assert "pages" in fields
    assert "pages.claim" in fields, fields
    assert "swept.url" in fields, fields
    # And a field with a banned word in it has to be caught wherever it is nested.
    planted = {
        "type": "object",
        "properties": {
            "pages": {"type": "array", "items": {"type": "object", "properties": {"body_font": {"type": "string"}}}}
        },
    }
    assert "pages.body_font" in _named_fields(planted)


def test_the_project_field_names_the_tool_that_actually_makes_a_project(tmp_path: Path) -> None:
    """Six of them named `ppt_ingest`, which on this route is optional and not first.

    A model reading "the project, as given to ppt_ingest" and finding no project has
    one place to go, and it is the wrong one: `ppt_ingest` is `required=False` here
    and refuses a deck holding no sources, while `ppt_prepare` is the first required
    stage and the call that creates the directory. Asserted against the route rather
    than against the string, so renaming the entry stage moves the check with it.
    """
    profile = registry.get(registry.DEFAULT)
    entry = next(stage.tool for stage in profile.stages if stage.required and stage.tool)
    offences = []
    for tool in build_ppt_tools(tmp_path):
        said = ((tool.parameters.get("properties") or {}).get("project") or {}).get("description") or ""
        named = {name for name in profile.tools if name and name != tool.name and name in said}
        if named - {entry}:
            offences.append(f"{tool.name}.project points at {sorted(named - {entry})} rather than {entry}")
    assert not offences, "\n  ".join(["a project field names the wrong tool:", *offences])


def test_the_view_budget_reaches_both_sides_of_the_call_from_one_setting(tmp_path: Path) -> None:
    """`tools.ppt.viewsPerCall` has to arrive as one number, not as two that agree.

    It was two hardcoded constants, 1 in the tool and 3 in the stage, and the tool's
    won: a nineteen-page deck took nineteen builds to look at once. Assembly is where a
    setting becomes a stage, so this is where a knob that is declared and never read
    would show -- the failure this module's docstring is about.
    """
    from raven_ppt.stages.build import BATCH_VIEWS

    for wanted in (1, 3, 7):
        build = next(t for t in build_ppt_tools(tmp_path, views_per_call=wanted) if t.name == "ppt_build")
        assert build.stage.views_per_call == wanted
        assert build.parameters["properties"]["slides"]["maxItems"] == wanted

    unset = next(t for t in build_ppt_tools(tmp_path) if t.name == "ppt_build")
    assert unset.stage.views_per_call == BATCH_VIEWS


def test_the_configured_default_is_the_one_the_stage_ships_with() -> None:
    """Two spellings of the same default, so they get a test rather than a comment.

    `renderDpi` spells 144 in the slice, in assembly and in `DeckViews`, and nothing
    checks that those stay the same number. This one is load-bearing in a way that one
    is not: it is the batch size the unseen gate is written from, so a slice default
    that drifted from the stage's would change what a deck must show to publish.
    """
    from raven_ppt.plugin.config import EngineConfig
    from raven_ppt.stages.build import BATCH_VIEWS

    assert EngineConfig().views_per_call == BATCH_VIEWS


def test_a_view_budget_outside_the_range_is_refused_not_silently_clamped() -> None:
    """Zero renders would publish a deck nobody looked at; the gate reads the same
    number, so it would also record nothing and refuse forever. Above twelve the reply
    approaches the content-block ceiling an endpoint has already refused. The fork
    said this with a pydantic range on tools.ppt; the plugin slice says it in
    EngineConfig.from_slice, whose raise the factories turn into the fail-closed
    sentinel hook (tests/test_ppt_engine_plugin.py pins that seat)."""
    from raven_ppt.plugin.config import EngineConfig

    assert EngineConfig.from_slice({"viewsPerCall": 12}).views_per_call == 12
    assert EngineConfig.from_slice({"readerEffort": "low"}).reader_effort == "low"
    assert EngineConfig.from_slice({}).reader_effort == "", "empty means the provider's own default"
    with pytest.raises(ValueError):
        EngineConfig.from_slice({"readerEffort": 3})
    for bad in (0, -1, 13):
        with pytest.raises(ValueError):
            EngineConfig.from_slice({"viewsPerCall": bad})
