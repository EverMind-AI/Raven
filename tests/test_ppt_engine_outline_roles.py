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

from raven_ppt.contracts.outline import PagePlan
from raven_ppt.tools.outline import (
    PAGE_ROLES,
    PptOutlineTool,
    _declared_roles,
    _page_line,
)


def _schema() -> dict[str, Any]:
    return PptOutlineTool(workspace=Path("/tmp")).parameters["properties"]["pages"]["items"]


def _deck(tmp_path: Path, *, low: int = 1, high: int = 6):
    """A project with the one thing `ppt_outline` refuses without: a brief."""
    from raven_ppt.contracts import DeckBrief, PageBudget, Project, brief_path, write_brief

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
    from raven_ppt.services.template import menu

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
    assert role["enum"] == [*PAGE_ROLES, None]
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


async def test_the_accepted_outline_names_the_tool_that_runs_the_program(tmp_path: Path) -> None:
    """The last ask an accepted outline makes is the instruction the author acts on,
    and it said "write the program" with no tool attached to it. One run took that
    sentence to a shell: 288 byte-identical exec calls, no build.py, no ppt_build.
    """
    body = await _reply(tmp_path, _pages(None, None, None))

    said = body["next_step"]
    assert "write the program" in said
    assert "deck/build/build.py" in said
    assert "ppt_build" in said


async def test_a_plan_naming_no_roles_reads_as_it_always_did(tmp_path: Path) -> None:
    """The line the field must not cross: an outline that says nothing about roles is
    the outline this tool answered before there was a field to say it in."""
    body = await _reply(tmp_path, _pages(None, None, None))

    assert body["pages"] == [f"{number}. Page {number} says something" for number in (1, 2, 3)]
    assert not any(" -- " in line for line in body["pages"])


async def test_a_page_that_says_no_number_keeps_its_role_at_its_place(tmp_path: Path) -> None:
    """Omitting `page` is a supported path now -- the plan numbers such a page by its
    place in the list -- and the roles are read off the same entries. Keyed by the
    number the entry stated, a cover with no number filed its role under page 0: the
    reply showed no `-- cover`, and the layout-spread reading counted a structural page
    as content. The same fallback, so the two readings name the same page."""
    planned = _pages("cover", None, "closing")
    for entry in planned:
        del entry["page"]

    body = await _reply(tmp_path, planned)

    assert body["pages"][0].endswith("  -- cover"), body["pages"]
    assert " -- " not in body["pages"][1]
    assert body["pages"][2].endswith("  -- closing"), body["pages"]


async def test_the_recorded_plan_is_the_same_file_either_way(tmp_path: Path) -> None:
    """Where the field stops, pinned rather than assumed.

    `write_outline` stores what `PagePlan` holds and `load_outline` rebuilds only the
    fields it names, so a role recorded on the plan would be written and never read
    back -- the road `section` is already on. Until both are taught the field, a role
    is the reply's and the file is byte for byte what it was.
    """
    from raven_ppt.contracts import outline_path

    with_roles = _deck(tmp_path / "a")
    await PptOutlineTool(tmp_path / "a").execute(project="deck", takeaway="t", pages=_pages("cover", None, "closing"))
    without = _deck(tmp_path / "b")
    await PptOutlineTool(tmp_path / "b").execute(project="deck", takeaway="t", pages=_pages(None, None, None))

    assert outline_path(with_roles).read_text(encoding="utf-8") == outline_path(without).read_text(encoding="utf-8")


def test_a_word_outside_the_four_is_refused_before_the_tool_runs() -> None:
    """By the registry, against the schema, which is where every other tool's
    vocabulary is checked -- so the outline stage needs no check of its own.
    The fork said this through its Tool.validate_params method; trunk keeps the
    same walk as a free function the registry calls (raven.agent.tools.params),
    so the pin drives that."""
    from raven.agent.tools.params import validate_params

    tool = PptOutlineTool(workspace=Path("/tmp"))
    call = {"project": "deck", "takeaway": "t", "pages": [{"page": 1, "claim": "c", "role": "intro"}]}

    assert validate_params(tool.parameters, {**call, "pages": [{"page": 1, "claim": "c", "role": "cover"}]}) == []
    refused = validate_params(tool.parameters, call)
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


async def test_a_page_saying_null_for_what_it_has_no_answer_to_is_accepted(tmp_path: Path) -> None:
    """Every optional page field takes null as it takes an absent key.

    A model with nothing to put in `anti_pattern` writes null as readily as it leaves
    the key out, and both readers already treat the two alike. Declared `string`
    alone, one null refused the whole outline through the registry, once per page --
    three live runs lost an outline round to it (amber twice, a Shanghai deck once),
    each costing a minutes-long re-plan. The required pair, `page` and `claim`, still
    refuses null: a page has to be numbered and has to say something.
    """
    from raven.agent.tools.registry import ToolRegistry

    _deck(tmp_path)
    pages = _pages("cover", None)
    pages[1].update(
        carries=None,
        layout=None,
        layers=None,
        anti_pattern=None,
        figures=None,
        says=None,
        section=None,
        role=None,
        needs=None,
    )

    registry = ToolRegistry()
    registry.register(PptOutlineTool(tmp_path))
    call = {"project": "deck", "takeaway": "the audience must believe this one thing", "pages": pages}
    body = str(await registry.execute("ppt_outline", call))

    assert not body.startswith("Error"), body
    assert len(json.loads(body)["pages"]) == 2

    refused = str(await registry.execute("ppt_outline", {**call, "pages": [{**pages[0], "claim": None}, pages[1]]}))
    assert refused.startswith("Error") and "pages[0].claim should be string" in refused


async def test_pages_that_say_no_number_are_numbered_by_their_place_in_the_list(tmp_path: Path) -> None:
    """The amber run wrote every page without `page` and lost the whole outline to
    `missing required pages[N].page`; the numbering check demands 1..n in order anyway,
    so the number says nothing the place in the list does not."""
    from raven.agent.tools.registry import ToolRegistry

    _deck(tmp_path)
    pages = _pages("cover", None)
    for page in pages:
        page.pop("page", None)

    registry = ToolRegistry()
    registry.register(PptOutlineTool(tmp_path))
    body = str(await registry.execute("ppt_outline", {"project": "deck", "takeaway": "one thing", "pages": pages}))

    assert not body.startswith("Error"), body
    listed = json.loads(body)["pages"]
    assert [line.split(".")[0] for line in listed] == ["1", "2"]


def test_the_declared_schema_takes_null_in_every_optional_page_field_as_json_schema(tmp_path: Path) -> None:
    """The nullable promise holds under a standards-compliant validator, not only Raven's.

    The parameters reach a provider as JSON Schema, where `type` and `enum` apply
    together: `["string", "null"]` on `layout` promised nothing while its enum listed
    only the structure ids, so a provider validating the call refused the null that
    Raven's own validator had let through.
    """
    from jsonschema import Draft202012Validator

    schema = PptOutlineTool(tmp_path).parameters
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    page = {
        "page": 1,
        "claim": "the audience must believe this one thing",
        "carries": None,
        "layout": None,
        "layers": None,
        "anti_pattern": None,
        "figures": None,
        "says": None,
        "section": None,
        "role": None,
        "needs": None,
    }
    call = {"project": "deck", "takeaway": "the audience must believe this one thing", "pages": [page]}

    errors = [error.message for error in validator.iter_errors(call)]
    assert not errors, errors
    assert list(validator.iter_errors({**call, "pages": [{**page, "claim": None}]}))


def test_a_type_naming_two_kinds_casts_and_validates_as_either() -> None:
    """Either named type passes, an unnamed one does not, and null is a type.

    Tested through a schema of its own rather than through the outline's, so that
    what is checked is the rule and not one field that happens to follow it. The
    fork pinned this on its Tool methods; trunk's walk lives in
    raven.agent.tools.params, patched with the fork's type-list branches by this
    wave, and the registry applies it to every call.
    """
    from raven.agent.tools.params import cast_params, validate_params

    schema = {
        "type": "object",
        "properties": {"maybe": {"type": ["integer", "null"]}},
    }

    assert cast_params(schema, {"maybe": 3}) == {"maybe": 3}
    assert cast_params(schema, {"maybe": None}) == {"maybe": None}
    # Cast against the type that is not null, as a single-typed field would be.
    assert cast_params(schema, {"maybe": "3"}) == {"maybe": 3}

    assert validate_params(schema, {"maybe": 3}) == []
    assert validate_params(schema, {"maybe": None}) == []
    assert validate_params(schema, {"maybe": [1]}) != []


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

    from raven_ppt.tools.prepare import PptPrepareTool

    assert list(inspect.signature(PptOutlineTool.__init__).parameters)[1:] == ["workspace"]
    assert PptOutlineTool.timeout_seconds < PptPrepareTool.timeout_seconds
