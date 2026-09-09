"""Bundled light templates available when a task has no user template."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DefaultTemplate:
    filename: str
    tags: tuple[str, ...]
    description: str

    @property
    def path(self) -> Path:
        return Path(__file__).parents[2] / "assets" / "templates" / self.filename

    def prompt_line(self) -> str:
        return f"- {self.filename} | tags: {', '.join(self.tags)} | {self.description}"


DEFAULT_TEMPLATES: tuple[DefaultTemplate, ...] = (
    DefaultTemplate(
        "beige_geometric_general_report.pptx",
        (
            "fallback",
            "default",
            "light",
            "minimal",
            "geometric",
            "warm",
            "beige",
            "general-purpose",
            "report",
            "review",
            "strategy",
            "research",
            "comparison",
            "competitive-analysis",
            "technology",
            "product",
        ),
        "sand and cream with orange, ochre and olive geometry; the general fallback, and with 23 example pages the widest choice of prototypes here",
    ),
    DefaultTemplate(
        "blue_minimal_general_analysis.pptx",
        (
            "light",
            "minimal",
            "analysis",
            "general",
            "blue",
            "illustrated",
            "technology",
            "product",
            "comparison",
        ),
        "white and royal blue with a flat black-and-yellow illustration; general-purpose analysis and recommendation decks, 15 example pages",
    ),
    DefaultTemplate(
        "gold_panel_year_end_summary.pptx",
        (
            "light",
            "editorial",
            "warm",
            "gold",
            "beige",
            "summary",
            "year-end",
            "review",
            "business",
        ),
        "cream panels banded in gold with concentric arcs; year-end and annual summaries, 25 example pages, the deepest set here",
    ),
    DefaultTemplate(
        "warm_bauhaus_quarterly_review.pptx",
        (
            "light",
            "geometric",
            "bauhaus",
            "warm",
            "colourful",
            "quarterly",
            "summary",
            "review",
            "business",
        ),
        "blush ground with a navy, red and amber Bauhaus tile collage; quarterly reviews and progress reports, 18 example pages",
    ),
    DefaultTemplate(
        "amber_wave_quarterly_summary.pptx",
        (
            "light",
            "warm",
            "amber",
            "photographic",
            "summary",
            "quarterly",
            "review",
            "business",
        ),
        "amber waves over a desk photograph; quarterly and periodic summaries with a softer register, 18 example pages",
    ),
    DefaultTemplate(
        "teal_illustrated_work_analysis.pptx",
        (
            "light",
            "illustrated",
            "teal",
            "friendly",
            "analysis",
            "planning",
            "process",
            "work",
        ),
        "cream and teal with a flat collaboration illustration; work analysis, process and planning decks, 15 example pages",
    ),
    DefaultTemplate(
        "mint_memphis_thesis_defense.pptx",
        (
            "light",
            "memphis",
            "geometric",
            "mint",
            "yellow",
            "academic",
            "thesis",
            "defence",
            "research",
        ),
        "white with mint and yellow Memphis shapes; thesis defences and academic progress reports, 20 example pages",
    ),
    DefaultTemplate(
        "red_chinese_traditional_culture.pptx",
        (
            "light",
            "chinese",
            "traditional",
            "red",
            "ornamental",
            "culture",
            "heritage",
            "history",
        ),
        "vermilion with a line-drawn landscape and classical borders; traditional culture, heritage and history, 15 example pages",
    ),
)


def default_template_catalog() -> tuple[DefaultTemplate, ...]:
    return tuple(template for template in DEFAULT_TEMPLATES if template.path.is_file())


# The content pages worth borrowing, chosen by the maintainer off renders of all 105
# content pages the bundled templates ship: the ones whose composition a deck built on
# any other bundled template can carry, once its colours and master follow the deck.
# Measured on four cross-template clones rendered side by side with their sources: every
# fill on these pages is a theme colour or white, so a borrowed page arrives in the
# deck's own palette with nothing of its source's showing but the arrangement.
REFERENCE_PAGES: dict[str, tuple[int, ...]] = {
    "amber_wave_quarterly_summary": (4, 5, 6, 8, 9, 10, 12, 13, 14, 16, 17),
    "beige_geometric_general_report": (17, 19),
    "blue_minimal_general_analysis": (7,),
    "gold_panel_year_end_summary": (4, 5, 6, 7, 9, 12, 13, 15, 20, 21),
    "mint_memphis_thesis_defense": (6, 9),
    "teal_illustrated_work_analysis": (5, 6, 7, 10, 13),
    "warm_bauhaus_quarterly_review": (6, 8, 9, 10, 14),
}

# How many pictures each reference page carries that are drawings rather than
# photographs, painted in its own template's accents. Nothing recolours a bitmap, so
# these arrive in the source's palette whatever deck they land in -- a page of teal
# cartoons on an amber deck -- while the photographs the other pages carry are
# placeholders an author replaces anyway. Measured over the 19 images the reference
# pages hold, by the share of the image its commonest colour covers (0.51 to 0.84 on
# these, 0.002 to 0.06 on the photographs beside them) and confirmed against the
# cross-template renders, where the drawings are the one thing that did not follow the
# deck. Absent means photographs or nothing.
REFERENCE_ARTWORK: dict[str, dict[int, int]] = {
    "teal_illustrated_work_analysis": {6: 1, 7: 4, 10: 1, 13: 1},
}

# Under this, white type on a fill of that colour stops being type. The number is
# `measure.contrast.UNREADABLE_RATIO`, restated here rather than imported because this
# module must not depend on the measurement package; the two are checked against each
# other in the tests.
#
# It matters at borrowing time because it is the one thing a borrowed page cannot
# bring with it. Measured over all 252 cross-template clones of these pages: 28 came
# out with copy under that ratio that read above it in its own template, and 19 of the
# 28 landed in the one bundled template whose accent1 renders white at 1.88:1. That
# template's own pages set dark ink on that fill instead -- its page 8 reads 7.11:1 on
# the same colour -- so what the clone carries across is a habit that is right in the
# seven templates whose accent1 is dark and wrong in the eighth. The ink is stated on
# the run, not derived from the ground, so nothing recolours it.
ACCENT_READS_WHITE = 2.0


def templates_dir() -> Path:
    """Where the bundled templates live on this install."""
    return Path(__file__).parents[2] / "assets" / "templates"


def bundled_path(stem: str) -> Path | None:
    """The bundled template file named by `stem`, or None when none ships under that name."""
    name = str(stem or "").strip().removesuffix(".pptx")
    if not name:
        return None
    path = templates_dir() / f"{name}.pptx"
    return path if path.is_file() else None


def reference_pages(*, except_stem: str = "") -> list[tuple[str, int]]:
    """(template stem, page) for every reference page on disk, the bound template's excluded.

    A deck borrows from the other templates: the bound one's own pages are already in
    its menu, numbered as the author sees them.
    """
    skip = str(except_stem or "").removesuffix(".pptx")
    found = []
    for stem, pages in REFERENCE_PAGES.items():
        if stem == skip or bundled_path(stem) is None:
            continue
        found.extend((stem, page) for page in pages)
    return found


def reference_artwork(stem: str, page: int) -> int:
    """How many drawings in `stem`'s own colours that reference page carries.

    0 for the pages that carry photographs or nothing: a photograph is a placeholder
    the author was going to replace, and saying so about one would spend the caveat
    on the ordinary case.
    """
    return REFERENCE_ARTWORK.get(str(stem or "").removesuffix(".pptx"), {}).get(int(page), 0)


def default_template_prompt() -> str:
    available = default_template_catalog()
    if not available:
        return "No bundled default templates are installed."
    return (
        "Bundled default templates are available because the user did not provide a template. "
        "Choose exactly one filename below for the `template` field when its tags fit the subject. "
        "Do not invent a path or choose a dark or irrelevant style:\n"
        + "\n".join(template.prompt_line() for template in available)
    )


def find_default_template(filename: str) -> DefaultTemplate | None:
    return next((template for template in default_template_catalog() if template.filename == filename), None)


def fallback_default_template() -> DefaultTemplate | None:
    available = default_template_catalog()
    return available[0] if available else None
