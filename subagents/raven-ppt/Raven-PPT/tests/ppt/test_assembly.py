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

    `ppt_outline` nests a page's table plan two levels under `pages`, so its
    `table_plan.columns` is the shape the old check was blind to and the cheapest
    proof the new one is not.
    """
    tools = {tool.name: tool for tool in build_ppt_tools(tmp_path)}
    outline = tools.get("ppt_outline")
    if outline is None:
        pytest.skip("the outline tool is not registered on the default route")
    fields = _named_fields(outline.parameters)
    assert "pages" in fields
    assert "pages.claim" in fields, fields
    assert "pages.table_plan.columns" in fields, fields
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
