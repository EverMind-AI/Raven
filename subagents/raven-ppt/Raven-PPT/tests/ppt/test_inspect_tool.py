from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.base import ToolResult
from raven.ppt.contracts import Project
from raven.ppt.services.ingest import CATALOGUE_FILE
from raven.ppt.tools.inspect import MAX_CAPTION_CHARS, PptFigureInspectTool, _label, _vetted


class _Views:
    def data_uri(self, path: Path) -> str:
        return "data:image/png;base64,AAAA"


class _Composer:
    def __init__(self, caption: str = "A three-stage memory lifecycle architecture.") -> None:
        self.calls = 0
        self.caption = caption
        self.briefs: list[str] = []

    async def ask(self, system, parts, *, max_tokens):
        self.calls += 1
        self.briefs.append(system)
        return json.dumps({"visual_caption": self.caption})


def _catalogue(project: Project, *, caption: str | None = None) -> None:
    project.ingest_dir.mkdir(parents=True, exist_ok=True)
    project.figures_dir.mkdir(parents=True, exist_ok=True)
    (project.figures_dir / "arch.png").write_bytes(b"PNG")
    (project.ingest_dir / CATALOGUE_FILE).write_text(
        json.dumps(
            {
                "schema": "raven.ppt.assets.v1",
                "assets": {
                    "arch-1": {
                        "file": "arch.png",
                        "kind": "image",
                        "width_px": 1200,
                        "height_px": 800,
                        "caption": caption,
                    }
                },
            }
        ),
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
    assert composer.calls == 0


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
