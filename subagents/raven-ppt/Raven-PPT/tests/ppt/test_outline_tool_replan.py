"""The outline's second pass: the plan re-written by the second model, once.

Driven with a fake composer so every branch is reachable without a model. Two
things it may never change, because they came from the brief the calling agent
agreed with the user: the deck's takeaway and how many pages it runs to. Which
page argues what, what carries it, and what it still needs are one decision and
are re-planned together -- rewriting the copy alone left a page written up into a
comparison still carrying the errand its one-line version had.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from raven.ppt.contracts import Outline, PagePlan, Project, load_brief  # noqa: F401
from raven.ppt.tools.outline import PptOutlineTool


class _Composer:
    """Answers with these lines for every page it is asked about."""

    def __init__(
        self,
        said: list[str] | None,
        pages: int = 12,
        skip: set[int] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.said = said
        self.pages = pages
        self.skip = skip or set()
        self.extra = extra or {}
        self.calls = 0
        self.seen: list[str] = []
        self.system: list[str] = []

    async def ask(self, system: str, parts: list[dict[str, Any]], *, max_tokens: int) -> str:
        self.calls += 1
        self.system.append(system)
        self.seen.append("".join(p.get("text", "") for p in parts))
        if self.said is None:
            return "not json"
        return json.dumps(
            {
                "pages": [
                    {"page": n, "says": self.said, **self.extra} for n in range(1, self.pages + 1) if n not in self.skip
                ]
            }
        )


class _State:
    """The deck state the plan is re-planned against."""

    def __init__(self, figures: tuple[str, ...] = ()) -> None:
        self.figures = tuple(type("F", (), {"figure_id": f})() for f in figures)


def _project(tmp_path: Path, materials: str = "A vendor reports 92.5 on the public benchmark.") -> Project:
    project = Project(workspace=tmp_path, slug="deck")
    project.ingest_dir.mkdir(parents=True, exist_ok=True)
    (project.ingest_dir / "materials.md").write_text(materials, encoding="utf-8")
    return project


def _thin_outline(pages: int = 12) -> Outline:
    return Outline(
        takeaway="t",
        pages=tuple(PagePlan(page=n, claim=f"claim {n}", says=("x" * 40,)) for n in range(1, pages + 1)),
    )


@pytest.mark.asyncio
async def test_the_plan_is_re_planned_whenever_a_second_model_is_configured(tmp_path: Path) -> None:
    """No longer conditional on a character count.

    It used to run only on a plan measured thin, which put a count in charge of
    whether the deck got planned properly -- and the same count is a full page in
    one language and a third of one in another.
    """
    composer = _Composer(["y" * 200, "z" * 200])
    tool = PptOutlineTool(tmp_path, composer=composer)
    filled = await tool._replan(_project(tmp_path), _thin_outline(), _State())

    assert composer.calls == 1, "one call for the deck: per page cost 20x and read worse"
    assert filled is not None
    assert filled.pages[0].says == ("y" * 200, "z" * 200)
    assert filled.pages[0].claim == "claim 1", "the claim is not the pass's to change"


@pytest.mark.asyncio
async def test_no_second_model_leaves_the_plan_as_submitted(tmp_path: Path) -> None:
    """The only thing that stops the pass, now that nothing measures the plan."""
    tool = PptOutlineTool(tmp_path, composer=None)

    assert await tool._replan(_project(tmp_path), _thin_outline(), _State()) is None


@pytest.mark.asyncio
async def test_a_page_the_reply_leaves_out_keeps_the_plan_it_had(tmp_path: Path) -> None:
    """A partial reply means "nothing better for that page", not "empty it"."""
    composer = _Composer(["y" * 200], skip={3})
    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), _thin_outline(), _State())

    assert filled is not None
    assert filled.pages[2].says == ("x" * 40,)
    assert filled.pages[1].says == ("y" * 200,)


@pytest.mark.asyncio
async def test_the_catalogue_is_what_a_figure_may_be_chosen_from(tmp_path: Path) -> None:
    """`figure` refuses an id that does not exist, so an invented one is dropped here."""
    composer = _Composer(["y" * 200], extra={"figures": ["fig-1", "made-up"]})
    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(
        _project(tmp_path), _thin_outline(), _State(("fig-1",))
    )

    assert "fig-1" in composer.seen[0], "the ids have to be readable from the call"
    assert filled is not None
    assert filled.pages[0].figures == ("fig-1",)


@pytest.mark.asyncio
async def test_the_outline_model_receives_figure_previews(tmp_path: Path) -> None:
    class _Views:
        def __init__(self) -> None:
            self.seen: list[Path] = []

        def data_uri(self, path: Path) -> str:
            self.seen.append(path)
            return "data:image/png;base64,AAAA"

    class _Figure:
        figure_id = "arch-1"
        file = "arch.png"

        def summary(self) -> str:
            return "arch-1 (architecture, 1200x800px)"

    project = _project(tmp_path)
    project.figures_dir.mkdir(parents=True, exist_ok=True)
    (project.figures_dir / "arch.png").write_bytes(b"PNG")
    composer = _Composer(["uses the architecture"], extra={"figures": ["arch-1"]})
    views = _Views()

    filled = await PptOutlineTool(tmp_path, composer=composer, views=views)._replan(
        project,
        _thin_outline(),
        type("State", (), {"figures": (_Figure(),)})(),
    )

    assert filled is not None
    assert views.seen == [project.figures_dir / "arch.png"]
    assert "Figure preview arch-1" in composer.seen[0]


@pytest.mark.asyncio
async def test_the_errand_is_re_planned_with_the_copy(tmp_path: Path) -> None:
    """A page written up into a comparison kept the errand its one-line version had."""
    composer = _Composer(["y" * 200], extra={"needs": "a diagram nobody has drawn: image_generate"})
    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), _thin_outline(), _State())

    assert filled is not None
    assert filled.pages[0].needs == "a diagram nobody has drawn: image_generate"


@pytest.mark.asyncio
async def test_a_pure_data_table_keeps_complete_cell_rows(tmp_path: Path) -> None:
    composer = _Composer(
        [],
        extra={
            "carries": "drawn pricing table",
            "table_plan": {
                "columns": ["Plan", "Price", "Adds"],
                "rows": [["Hobby", "Free", "10,000"], ["Pro", "$249", "500,000"]],
                "reading": "compare price and capacity",
            },
        },
    )

    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(
        _project(tmp_path), _thin_outline(), _State()
    )

    assert filled is not None
    assert filled.pages[0].says == ()
    assert filled.pages[0].table_plan == {
        "columns": ("Plan", "Price", "Adds"),
        "rows": (("Hobby", "Free", "10,000"), ("Pro", "$249", "500,000")),
        "reading": "compare price and capacity",
    }


@pytest.mark.asyncio
async def test_the_movement_a_page_belongs_to_comes_back_with_it(tmp_path: Path) -> None:
    composer = _Composer(["y" * 200], extra={"section": "where the argument turns"})
    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), _thin_outline(), _State())

    assert filled is not None
    assert filled.pages[0].section == "where the argument turns"


@pytest.mark.asyncio
async def test_a_replan_can_clear_a_prototype_for_free_composition(tmp_path: Path) -> None:
    composer = _Composer(["y" * 200], extra={"prototype": None})
    outline = replace(
        _thin_outline(),
        pages=tuple(replace(page, prototype=8) for page in _thin_outline().pages),
    )

    filled = await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), outline, _State())

    assert filled is not None
    assert all(page.prototype is None for page in filled.pages)


@pytest.mark.asyncio
async def test_the_language_is_named_in_the_call_not_referred_to(tmp_path: Path) -> None:
    """ "The deck's language" left it to be inferred, and three arms of an A/B drifted."""
    composer = _Composer(["y" * 200])
    await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), _thin_outline(), _State(), "中文")

    assert "中文" in composer.seen[0] or "中文" in composer.system[0]


@pytest.mark.asyncio
async def test_the_brief_states_no_character_count(tmp_path: Path) -> None:
    """Per point it became a quota (57 a point); per page it is still a count, and a
    count is a full page in Chinese and a third of one in English. So the brief says
    what a page has to carry and the render settles whether it fits."""
    composer = _Composer(["y" * 200])
    await PptOutlineTool(tmp_path, composer=composer)._replan(_project(tmp_path), _thin_outline(), _State(), "English")
    brief = composer.system[0]

    assert not re.search(r"\d{2,} characters", brief)
    assert "the claim is carried and no more" in brief


@pytest.mark.asyncio
async def test_the_checks_judge_the_outline_that_gets_persisted(tmp_path: Path, monkeypatch) -> None:
    """The checks ran on the submitted outline and the replan then rewrote their inputs,
    so a replan that cleared every prototype was recorded without being re-judged."""
    from raven.ppt.contracts import DeckBrief, PageBudget, brief_path, write_brief
    from raven.ppt.tools import outline as outline_module

    project = _project(tmp_path)
    write_brief(
        DeckBrief(language="English", audience="leadership", pages=PageBudget(low=1, high=12)),
        brief_path(project),
    )

    judged: list[tuple[int | None, ...]] = []
    real_structural = outline_module._structural

    def spy(outline: Outline):
        judged.append(tuple(page.prototype for page in outline.pages))
        return real_structural(outline)

    monkeypatch.setattr(outline_module, "_structural", spy)

    composer = _Composer(["y" * 200], pages=2, extra={"prototype": None})
    tool = PptOutlineTool(tmp_path, composer=composer)
    await tool.execute(
        project=project.slug,
        takeaway="the fleet doubled",
        pages=[
            {"page": 1, "claim": "cover", "says": ["x" * 40], "prototype": 8},
            {"page": 2, "claim": "closing", "says": ["x" * 40], "prototype": 9},
        ],
    )

    assert judged, "_structural was never reached"
    assert judged[-1] == (None, None), (
        "the checks judged the submitted prototypes %r, but the replan cleared them "
        "before the outline was persisted" % (judged[-1],)
    )
