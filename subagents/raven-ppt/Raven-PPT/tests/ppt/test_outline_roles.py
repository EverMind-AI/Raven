"""The part each page plays: declared in the plan, read back off the reply.

The vocabulary is not this stage's own and must not become a second one.
`template.menu` reads `cover`, `agenda`, `section` and `closing` off a template's own
example pages, `house_style` calls every page without one a content page, and the gate
registry warns about "a deck built inside a template that did not open, index, divide
or close in the template's own pages" -- so both ends of the route already spoke these
four words while the plan that runs between them had no way to say any of them.

What is checked here is that the two lists stay the same words, that the field is
offered and never demanded, that a plan naming roles reads them back, and that a plan
naming none comes out of the tool exactly as it did before the field existed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from raven.ppt.contracts.outline import PagePlan
from raven.ppt.tools.outline import (
    PAGE_ROLES,
    PptOutlineTool,
    _declared_roles,
    _page_line,
)


def _schema() -> dict[str, Any]:
    return PptOutlineTool(workspace=Path("/tmp")).parameters["properties"]["pages"]["items"]


def _deck(tmp_path: Path, *, low: int = 1, high: int = 6):
    """A project with the one thing `ppt_outline` refuses without: a brief."""
    from raven.ppt.contracts import DeckBrief, PageBudget, Project, brief_path, write_brief

    deck = Project(workspace=tmp_path, slug="deck")
    write_brief(DeckBrief(language="English", audience="a review", pages=PageBudget(low, high)), brief_path(deck))
    return deck


def _pages(*roles: str | None) -> list[dict[str, Any]]:
    """One page per argument, carrying the role it is given and two points either way.

    Two rather than one because a page planning a single line and nothing else is what
    `_thin_pages` reports, and a warning about density is not what these tests measure.
    """
    planned = []
    for number, role in enumerate(roles, start=1):
        page: dict[str, Any] = {
            "page": number,
            "claim": f"Page {number} says something",
            "says": [f"Show: the evidence for page {number}.", f"Explain: why page {number} holds."],
        }
        if role is not None:
            page["role"] = role
        planned.append(page)
    return planned


async def _reply(tmp_path: Path, pages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    _deck(tmp_path)
    body = await PptOutlineTool(tmp_path, **kwargs).execute(
        project="deck", takeaway="the audience must believe this one thing", pages=pages
    )
    return json.loads(body)


def test_the_roles_are_the_words_the_template_side_already_reads() -> None:
    """The whole point of the field, and the one way it can go wrong quietly.

    `menu._role` returns one of these four or "" for a content page. A fifth word
    invented here would be a plan describing a page the template side cannot recognise
    as the same kind of thing.
    """
    pytest.importorskip("pptx", reason="the template package imports python-pptx at module scope")
    from raven.ppt.services.template import menu

    assert set(PAGE_ROLES) == {menu.COVER, menu.AGENDA, menu.SECTION, menu.CLOSING}

    read_off_pages = {
        menu._role(1, "Title Slide", "A deck about something"),
        menu._role(2, "Title and Content", "Agenda"),
        menu._role(3, "Section Header", "Part one"),
        menu._role(4, "Title and Content", "Thank you"),
        menu._role(5, "Title and Content", "What the numbers say", blocks=6, longest=180),
    }
    assert read_off_pages - {""} <= set(PAGE_ROLES)


def test_the_schema_offers_a_role_and_never_asks_for_one() -> None:
    role = _schema()["properties"]["role"]

    assert "role" not in _schema()["required"]
    assert role["enum"] == list(PAGE_ROLES)
    for word in PAGE_ROLES:
        assert f"`{word}`" in role["description"], f"the schema names no meaning for {word}"
    assert "Leave it out" in role["description"], "nothing tells the author when a page has no role"


def test_the_role_states_no_physical_quantity() -> None:
    """A role says what a page is for. Where it sits and how big its type is are the
    engine's, and a schema that says otherwise is the defect the invariant names."""
    said = _schema()["properties"]["role"]["description"]

    assert not re.search(r"\b(inch|inches|EMU|px|pt|point|font)\b", said, re.IGNORECASE)


async def test_the_reply_says_what_part_each_page_plays(tmp_path: Path) -> None:
    body = await _reply(tmp_path, _pages("cover", "agenda", None, "closing"))

    assert body["pages"] == [
        "1. Page 1 says something  -- cover",
        "2. Page 2 says something  -- agenda",
        "3. Page 3 says something",
        "4. Page 4 says something  -- closing",
    ]


async def test_a_plan_naming_no_roles_reads_as_it_always_did(tmp_path: Path) -> None:
    """The line the field must not cross: an outline that says nothing about roles is
    the outline this tool answered before there was a field to say it in."""
    body = await _reply(tmp_path, _pages(None, None, None))

    assert body["pages"] == [f"{number}. Page {number} says something" for number in (1, 2, 3)]
    assert not any(" -- " in line for line in body["pages"])


async def test_the_recorded_plan_is_the_same_file_either_way(tmp_path: Path) -> None:
    """Where the field stops, pinned rather than assumed.

    `write_outline` stores what `PagePlan` holds and `load_outline` rebuilds only the
    fields it names, so a role recorded on the plan would be written and never read
    back -- the road `section` is already on. Until both are taught the field, a role
    is the reply's and the file is byte for byte what it was.
    """
    from raven.ppt.contracts import outline_path

    with_roles = _deck(tmp_path / "a")
    await PptOutlineTool(tmp_path / "a").execute(project="deck", takeaway="t", pages=_pages("cover", None, "closing"))
    without = _deck(tmp_path / "b")
    await PptOutlineTool(tmp_path / "b").execute(project="deck", takeaway="t", pages=_pages(None, None, None))

    assert outline_path(with_roles).read_text(encoding="utf-8") == outline_path(without).read_text(encoding="utf-8")


def test_a_word_outside_the_four_is_refused_before_the_tool_runs() -> None:
    """By the registry, against the schema, which is where every other tool's
    vocabulary is checked -- so the outline stage needs no check of its own."""
    tool = PptOutlineTool(workspace=Path("/tmp"))
    call = {"project": "deck", "takeaway": "t", "pages": [{"page": 1, "claim": "c", "role": "intro"}]}

    assert tool.validate_params({**call, "pages": [{"page": 1, "claim": "c", "role": "cover"}]}) == []
    refused = tool.validate_params(call)
    assert len(refused) == 1
    assert "pages[0].role" in refused[0]
    assert all(word in refused[0] for word in PAGE_ROLES)


def test_a_role_that_got_past_the_schema_is_dropped_rather_than_kept() -> None:
    """A caller that does not go through the registry -- a test, another tool -- is the
    only way one arrives, and a word nothing recognises is not a rhythm to report."""
    assert _declared_roles([{"page": 1, "role": "intro"}, {"page": 2, "role": "COVER "}]) == {2: "cover"}
    assert _declared_roles([{"page": 1, "claim": "c"}]) == {}
    assert _page_line(PagePlan(page=1, claim="c"), "") == "1. c"


async def test_a_page_naming_a_prototype_survives_the_route_to_the_tool(tmp_path: Path) -> None:
    """The registry casts a call before it runs it, and `prototype` names two types.

    Every other test here calls `execute` directly, which is why this went out: the
    fault was not in the tool but on the road to it. `prototype` is declared
    `["integer", "null"]` -- a page starts from a template example, or says outright
    that none fits -- and `cast_params` read `schema["type"]` as a string and indexed
    a dict with it, so a list reached `in` as a key and raised `unhashable type:
    'list'` before the tool saw the call.

    What that cost is the reason this test exists. Three end-to-end runs met it, and
    none of them could tell a broken tool from a rejected call: each read the message
    as a verdict on its own arguments, dropped `prototype`, added it back, probed with
    a two-page outline, and gave up without a deck. The field is asked for by both the
    tool's own description and the `house_page` gate, so every run reaches it.
    """
    from raven.agent.tools.registry import ToolRegistry

    _deck(tmp_path)
    pages = _pages("cover", None)
    pages[0]["prototype"] = 1
    pages[1]["prototype"] = None

    registry = ToolRegistry()
    registry.register(PptOutlineTool(tmp_path))
    body = str(
        await registry.execute(
            "ppt_outline",
            {"project": "deck", "takeaway": "the audience must believe this one thing", "pages": pages},
        )
    )
    assert "unhashable" not in body
    assert not body.startswith("Error")
    assert json.loads(body)["pages"]


def test_a_type_naming_two_kinds_casts_and_validates_as_either() -> None:
    """Either named type passes, an unnamed one does not, and null is a type.

    Tested through a schema of its own rather than through the outline's, so that
    what is checked is the rule and not one field that happens to follow it.
    """
    from raven.agent.tools.base import Tool

    class _Pair(Tool):
        name = "pair"
        description = "a field that is a number or explicitly nothing"
        parameters = {
            "type": "object",
            "properties": {"maybe": {"type": ["integer", "null"]}},
        }

        async def execute(self, **kwargs: Any) -> str:
            return ""

    tool = _Pair()
    assert tool.cast_params({"maybe": 3}) == {"maybe": 3}
    assert tool.cast_params({"maybe": None}) == {"maybe": None}
    # Cast against the type that is not null, as a single-typed field would be.
    assert tool.cast_params({"maybe": "3"}) == {"maybe": 3}

    assert tool.validate_params({"maybe": 3}) == []
    assert tool.validate_params({"maybe": None}) == []
    assert tool.validate_params({"maybe": [1]}) != []


def test_the_outline_is_a_validator_and_not_a_second_planner() -> None:
    """It used to carry a second model that re-planned all twenty pages inside one
    reply, against the whole material and every gathered figure, at a 32k token
    budget doubled on retry. Two end-to-end runs ended in that call -- one of them
    trimming its own payload to hunt for a size that fit, which is the shape the
    failure takes from the inside: a model reading a timeout as a verdict on what it
    sent. The tool takes a workspace and nothing else now, and its budget is a
    validator's rather than a model call's.
    """
    import inspect

    from raven.ppt.tools.prepare import PptPrepareTool

    assert list(inspect.signature(PptOutlineTool.__init__).parameters)[1:] == ["workspace"]
    assert PptOutlineTool.timeout_seconds < PptPrepareTool.timeout_seconds
