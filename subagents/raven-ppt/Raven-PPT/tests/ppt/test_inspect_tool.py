from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from raven.agent.tools.base import ToolResult
from raven.ppt.contracts import Project
from raven.ppt.services.ingest import CATALOGUE_FILE
from raven.ppt.tools._composer import ProviderComposer
from raven.ppt.tools.inspect import MAX_CAPTION_CHARS, PptFigureInspectTool, _label, _vetted


class _Views:
    def data_uri(self, path: Path) -> str:
        return "data:image/png;base64,AAAA"


class _Composer:
    def __init__(
        self,
        caption: str = "A three-stage memory lifecycle architecture.",
        review: str = "The plot is whole and its axis labels are readable.",
    ) -> None:
        self.calls = 0
        self.caption = caption
        self.review = review
        self.briefs: list[str] = []

    async def ask_with_failure(self, system, parts, *, max_tokens):
        self.calls += 1
        self.briefs.append(system)
        return json.dumps({"visual_review": self.review, "visual_caption": self.caption}), ""


def _catalogue(
    project: Project,
    names: tuple[str, ...] = ("arch-1",),
    *,
    caption: str | None = None,
    review: str | None = None,
) -> None:
    project.ingest_dir.mkdir(parents=True, exist_ok=True)
    project.figures_dir.mkdir(parents=True, exist_ok=True)
    assets = {}
    for name in names:
        (project.figures_dir / f"{name}.png").write_bytes(b"PNG")
        assets[name] = {
            "file": f"{name}.png",
            "kind": "image",
            "width_px": 1200,
            "height_px": 800,
            "caption": caption,
            **({"visual_review": review} if review else {}),
        }
    (project.ingest_dir / CATALOGUE_FILE).write_text(
        json.dumps({"schema": "raven.ppt.assets.v1", "assets": assets}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_inspection_adds_a_visual_caption_when_the_source_has_none(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer()

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert isinstance(result, ToolResult)
    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert saved["assets"]["arch-1"]["caption"] is None
    assert saved["assets"]["arch-1"]["visual_caption"] == "A three-stage memory lifecycle architecture."
    assert "visual_caption" in result.model_text


@pytest.mark.asyncio
async def test_source_caption_is_never_replaced_by_visual_inspection(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 3. Source-authored caption.")
    composer = _Composer()

    await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(project="deck", figures=["arch-1"])

    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert saved["assets"]["arch-1"]["caption"] == "Figure 3. Source-authored caption."
    assert "visual_caption" not in saved["assets"]["arch-1"]


@pytest.mark.asyncio
async def test_a_captioned_figure_is_still_looked_at(tmp_path: Path) -> None:
    """The delivered deck that made this a bug: a figure whose top edge was cut off in
    the paper's own page arrived with the paper's caption, the caption was the only
    condition on the look, and so nothing ever read the pixels that were placed."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 2: An illustration of the architecture of our CNN.")
    composer = _Composer(review="The top edge of the leftmost input block is cut off.")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert composer.calls == 1
    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert saved["assets"]["arch-1"]["visual_review"] == "The top edge of the leftmost input block is cut off."
    assert isinstance(result, ToolResult)
    assert "The top edge of the leftmost input block is cut off." in result.model_text


@pytest.mark.asyncio
async def test_a_captioned_figure_is_not_asked_for_a_second_caption(tmp_path: Path) -> None:
    """One call, two products, and the caption half of it is the half that is optional:
    asking for a caption a figure already has is how the inspected one came to overwrite
    the source's in the first place."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 2: An illustration of the architecture of our CNN.")
    composer = _Composer()

    await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(project="deck", figures=["arch-1"])

    brief = composer.briefs[0]
    assert "visual_review" in brief
    assert "visual_caption" not in brief


@pytest.mark.asyncio
async def test_a_figure_already_looked_at_is_not_looked_at_again(tmp_path: Path) -> None:
    """What may skip the look is evidence that one happened, and nothing else."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 2. Source-authored caption.", review="Reads whole and legible.")
    composer = _Composer()

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert composer.calls == 0
    assert isinstance(result, ToolResult)
    assert "Reads whole and legible." in result.model_text


@pytest.mark.asyncio
async def test_a_caption_a_bad_reply_cost_is_still_owed_at_the_next_look(tmp_path: Path) -> None:
    """The stored review is not evidence that the caption landed. A source-less figure
    whose first reply carried a good observation and a caption `_vetted` turned down had
    the review written and the review was the whole condition on the call, so the second
    look asked for nothing, wrote nothing, and no longer even said a caption was owed."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer(caption="Figure 3 shows a three-stage memory lifecycle.")
    tool = PptFigureInspectTool(tmp_path, _Views(), composer=composer)

    first = await tool.execute(project="deck", figures=["arch-1"])
    assert isinstance(first, ToolResult)
    assert "arch-1" in json.loads(first.model_text)["no_caption_written_for"]

    composer.caption = "a three-stage memory lifecycle drawn left to right"
    await tool.execute(project="deck", figures=["arch-1"])

    assert composer.calls == 2
    assert "visual_caption" in composer.briefs[1]
    assert "visual_review" not in composer.briefs[1]
    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))["assets"]["arch-1"]
    assert saved["visual_caption"] == "a three-stage memory lifecycle drawn left to right"
    assert saved["visual_review"] == "The plot is whole and its axis labels are readable."


@pytest.mark.asyncio
async def test_a_reply_that_omitted_the_caption_says_so(tmp_path: Path) -> None:
    """Silence about the caption reads as a figure that has one."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer(caption="")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert isinstance(result, ToolResult)
    assert "arch-1" in json.loads(result.model_text)["no_caption_written_for"]


@pytest.mark.asyncio
async def test_a_figure_nothing_looked_at_says_so(tmp_path: Path) -> None:
    """Silence about a figure reads as a figure that was looked at and found fine, which
    is the reading that let a cut-off render through."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 2. Source-authored caption.")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=None).execute(project="deck", figures=["arch-1"])

    assert isinstance(result, ToolResult)
    body = json.loads(result.model_text)
    assert "arch-1" in body["not_looked_at"]


@pytest.mark.asyncio
async def test_without_a_model_each_debt_is_reported_on_its_own(tmp_path: Path) -> None:
    """The no-model branch owed the same per-product answer the model-backed one got.
    A figure holding a review has been looked at, and a source-less one still owes its
    caption -- reporting the first as unread sends the author to redo what is done, and
    dropping the second is what leaves it source-less for good."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, review="a complete schematic, nothing cut off")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=None).execute(project="deck", figures=["arch-1"])

    assert isinstance(result, ToolResult)
    body = json.loads(result.model_text)
    assert "arch-1" not in (body.get("not_looked_at") or {}), "a stored review is a look that happened"
    assert "arch-1" in (body.get("no_caption_written_for") or {}), "a source-less figure still owes a caption"


@pytest.mark.asyncio
async def test_a_figure_file_that_cannot_be_read_reports_only_the_halves_still_owed(tmp_path: Path) -> None:
    """The fourth place one condition stood in for two products. With the review stored
    and the file gone, this branch reported the figure as never looked at and said nothing
    about the caption it still owed -- both answers inverted."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, review="a complete schematic, nothing cut off")
    (project.figures_dir / "arch-1.png").unlink()
    composer = _Composer(json.dumps({"visual_caption": "unused"}))

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    assert isinstance(result, ToolResult)
    body = json.loads(result.model_text)
    assert composer.calls == 0, "an unreadable file cannot be asked about"
    assert "arch-1" not in (body.get("not_looked_at") or {}), "a stored review is a look that happened"
    assert "arch-1" in (body.get("no_caption_written_for") or {}), "a source-less figure still owes a caption"


@pytest.mark.asyncio
async def test_an_inspection_that_observed_nothing_is_reported_as_nothing(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, caption="Figure 2. Source-authored caption.")
    composer = _Composer(review="")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert "visual_review" not in saved["assets"]["arch-1"]
    assert isinstance(result, ToolResult)
    assert "arch-1" in json.loads(result.model_text)["not_looked_at"]


@dataclass
class _Delta:
    content: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class _OneTimesOutOneGarbles:
    """arch-1's transport fails; arch-2 answers, late, with something that is not JSON."""

    def chat_stream(self, messages, *, model=None, max_tokens: int, temperature=None, **_kw):
        garbles = "arch-2" in str(messages)

        async def stream():
            if garbles:
                await asyncio.sleep(0.05)
                yield _Delta(content="here is the figure, described in prose")
                yield _Delta(finish_reason="stop")
                return
            await asyncio.sleep(0.01)
            raise TimeoutError("read timed out after 120s")
            yield  # pragma: no cover

        return stream()


@pytest.mark.asyncio
async def test_each_figure_reports_the_reason_its_own_look_came_back_empty(tmp_path: Path) -> None:
    """Two figures, one composer, one `failure` field between them. The figure whose reply
    was merely malformed was reported as a figure the gateway never answered, which sends
    the author to recover a transport that was working."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project, ("arch-1", "arch-2"), caption="Figure 2. Source-authored caption.")
    composer = ProviderComposer(provider=_OneTimesOutOneGarbles())

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1", "arch-2"]
    )

    assert isinstance(result, ToolResult)
    unseen = json.loads(result.model_text)["not_looked_at"]
    assert unseen["arch-1"] == "inspection could not be reached: TimeoutError: read timed out after 120s"
    assert unseen["arch-2"] == "inspection replied with something that is not JSON"


@pytest.mark.asyncio
async def test_the_brief_names_the_shape_the_run_that_broke_this_got_wrong(tmp_path: Path) -> None:
    """A banner was captioned as the architecture of the product it advertises. The
    prompt cannot guarantee that will not happen again, but it can at least say so."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer()

    await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(project="deck", figures=["arch-1"])

    brief = composer.briefs[0]
    assert "banner" in brief
    assert str(MAX_CAPTION_CHARS) in brief


@pytest.mark.asyncio
async def test_a_caption_longer_than_one_is_not_written_to_the_catalogue(tmp_path: Path) -> None:
    """The brief asks for one concise caption, so a paragraph is checkable rather than
    arguable -- and it is the string that would land under a figure."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer(caption="word " * 100)

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert "visual_caption" not in saved["assets"]["arch-1"]
    assert isinstance(result, ToolResult)
    assert "no_caption_written_for" in result.model_text


@pytest.mark.asyncio
async def test_a_caption_that_numbers_the_figure_is_not_written(tmp_path: Path) -> None:
    """A number is the source's own label, `source_label` is the field that holds one,
    and the citation gate reads it from there -- so a number invented by looking at the
    picture lets a page cite a figure with nothing to check the citation against."""
    project = Project(workspace=tmp_path, slug="deck")
    _catalogue(project)
    composer = _Composer(caption="Figure 3 shows a three-stage memory lifecycle.")

    result = await PptFigureInspectTool(tmp_path, _Views(), composer=composer).execute(
        project="deck", figures=["arch-1"]
    )

    saved = json.loads((project.ingest_dir / CATALOGUE_FILE).read_text(encoding="utf-8"))
    assert "visual_caption" not in saved["assets"]["arch-1"]
    assert isinstance(result, ToolResult)
    assert "numbered the figure" in result.model_text


@pytest.mark.parametrize(
    "caption",
    [
        "a table 3 rows tall beside a bar chart",
        "柱状图 3 个分组，纵轴为准确率",
        "a three-stage pipeline drawn left to right",
    ],
)
def test_a_caption_describing_a_count_is_not_a_figure_number(caption: str) -> None:
    """The misfire that would cost the author a caption for nothing: a digit after
    "table", or after the character that ends every Chinese word for a chart kind, is
    counting something rather than citing a figure."""
    kept, refused = _vetted(caption)

    assert kept == caption
    assert refused == ""


@pytest.mark.parametrize(
    "caption", ["Figure 3 shows the pipeline", "see Fig. 4", "Table 1 lists the ablations", "图 2"]
)
def test_a_caption_citing_a_figure_number_is_refused(caption: str) -> None:
    kept, refused = _vetted(caption)

    assert kept == ""
    assert "numbered the figure" in refused


def test_what_looking_showed_is_labelled_as_not_a_caption() -> None:
    """A sentence about the render is not a line to print under the figure, and the two
    reach the author in the same block."""
    entry = {
        "file": "arch.png",
        "caption": "Figure 2. Source-authored caption.",
        "visual_review": "The top edge of the leftmost input block is cut off.",
        "width_px": 1200,
    }

    said = _label("arch-1", entry)

    assert "which is not a caption: The top edge of the leftmost input block is cut off." in said


def test_the_two_captions_are_labelled_for_what_each_one_is() -> None:
    """What the model is handed beside the pixels. An `elif` used to hide the inspected
    caption whenever a source caption existed, which is the same collapse in a second
    place -- and neither line may read as the other's kind of claim."""
    entry = {
        "file": "arch.png",
        "caption": "Figure 3. Source-authored caption.",
        "visual_caption": "a three-stage pipeline drawn left to right",
        "width_px": 1200,
    }

    said = _label("arch-1", entry)

    assert "its source's caption, quotable and creditable: Figure 3. Source-authored caption." in said
    assert "never quote or credit it: a three-stage pipeline drawn left to right" in said
