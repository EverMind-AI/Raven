"""A figure's two captions, as the reader of a deck's state is shown them.

`caption` is what the figure's own source printed under it; `visual_caption` is a
sentence a model wrote by looking at the pixels. `Figure.summary` used to render
`caption or visual_caption`, so the second arrived in a prompt, and in the citation
reasoning downstream, indistinguishable from the first.

The case these are built from is real. A marketing banner reading "OPEN SKILLS FOR
REAL AGENTS / Curate the ecosystem. Retrieve what matters. / 821K -> 96,401 open
skills, curated" was inspected, captioned as the architecture of a system, and the
built page then credited the picture to a product.
"""

from __future__ import annotations

from pathlib import Path

from raven_ppt.contracts import Project
from raven_ppt.services.state import (
    CAPTION_LEGEND,
    INSPECTED_CAPTION_LEAD,
    SOURCE_CAPTION_LEAD,
    DeckState,
    Figure,
)

BANNER_CAPTION = (
    "该信息图展示了包含技能库构建（SkillCorpus）、任务检索匹配以及智能体执行验证全流程的开放技能处理系统架构图。"
)


def _banner() -> Figure:
    """The figure as `figures.json` recorded it: no source caption, no label."""
    return Figure(
        figure_id="skillcorpus_paper-1b9bc44f2e",
        kind="image",
        visual_caption=BANNER_CAPTION,
        file="skillcorpus_paper.jpg",
        width_px=1672,
        height_px=941,
    )


def test_an_inspected_caption_is_never_presented_as_the_sources_own_words() -> None:
    """The defect, stated as a test: the banner's caption is a reading of a picture
    and the summary may not offer it as anything a source said."""
    said = _banner().summary()

    assert BANNER_CAPTION[:20] in said, "the caption is still shown -- it is the only one there is"
    assert INSPECTED_CAPTION_LEAD in said
    assert SOURCE_CAPTION_LEAD not in said
    assert f'"{BANNER_CAPTION}"' not in said, "quoting it makes it read as the words of whoever wrote the figure"


def test_a_source_caption_is_quoted_and_credited() -> None:
    figure = Figure(figure_id="fig_3", kind="figure", label="Figure 3", caption="ablation on memory length")

    said = figure.summary()

    assert f'{SOURCE_CAPTION_LEAD}: "ablation on memory length"' in said
    assert INSPECTED_CAPTION_LEAD not in said


def test_both_captions_reach_the_reader_when_a_figure_carries_both() -> None:
    """`caption or visual_caption` returned one string, so whichever came second was
    simply gone -- and the disagreement worth noticing was gone with it."""
    figure = Figure(
        figure_id="fig_3",
        kind="figure",
        caption="Mem0 memory extraction",
        visual_caption="a three-stage pipeline drawn left to right",
    )

    said = figure.summary()

    assert "Mem0 memory extraction" in said
    assert "a three-stage pipeline drawn left to right" in said
    assert said.index(SOURCE_CAPTION_LEAD) < said.index(INSPECTED_CAPTION_LEAD)


def test_the_figure_list_says_which_caption_is_which(tmp_path: Path) -> None:
    state = DeckState(project=Project(workspace=tmp_path, slug="deck"), figures=(_banner(),))

    assert CAPTION_LEGEND in state.summary()


def test_the_legend_stays_out_of_a_deck_whose_figures_were_never_inspected(tmp_path: Path) -> None:
    """It explains a distinction that is not on the page: every caption here is a
    source's, so a line telling a reader how to tell them apart is noise."""
    figure = Figure(figure_id="fig_3", kind="figure", caption="ablation on memory length")
    state = DeckState(project=Project(workspace=tmp_path, slug="deck"), figures=(figure,))

    said = state.summary()

    assert "Figures extracted (1)" in said
    assert CAPTION_LEGEND not in said
