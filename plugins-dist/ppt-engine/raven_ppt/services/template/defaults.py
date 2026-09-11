"""Bundled templates available when a task has no user template: eight light, two dark."""

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
    DefaultTemplate(
        "black_circuit_tech_launch.pptx",
        (
            "dark",
            "technology",
            "tech",
            "black",
            "orange",
            "photographic",
            "circuit",
            "product",
            "launch",
            "release",
            "ai",
            "digital",
            "industry",
            "analysis",
        ),
        "near-black circuit-board photography under an orange glow; dark, for technology product launches, AI, hardware and digital-industry decks, 34 example pages",
    ),
    DefaultTemplate(
        "green_aurora_tech_trends.pptx",
        (
            "dark",
            "technology",
            "tech",
            "green",
            "black",
            "futuristic",
            "aurora",
            "innovation",
            "trends",
            "digital",
            "energy",
            "sustainability",
            "strategy",
        ),
        "near-black with green aurora light waves and no photographs; dark, for technology trend, innovation and energy or sustainability decks, 28 example pages",
    ),
)


def default_template_catalog() -> tuple[DefaultTemplate, ...]:
    return tuple(template for template in DEFAULT_TEMPLATES if template.path.is_file())


# The content pages worth borrowing, chosen by the maintainer off renders of all 105
# content pages the bundled templates ship: the ones whose composition a deck built on
# any other bundled template can carry, once its colours and master follow the deck.
# Measured on four cross-template clones rendered side by side with their sources: every
# fill on these pages is a theme colour or white, so a borrowed page arrives in the
# deck's own palette with nothing of its source's showing but the arrangement. The two
# dark templates' pages were measured the other way round, cloned into two light
# templates: their translucent panels follow the ground they land on, and the orange 3D
# renders of the first stay orange.
REFERENCE_PAGES: dict[str, tuple[int, ...]] = {
    "amber_wave_quarterly_summary": (4, 5, 6, 8, 9, 10, 12, 13, 14, 16, 17),
    "beige_geometric_general_report": (17, 19),
    "blue_minimal_general_analysis": (7,),
    "gold_panel_year_end_summary": (4, 5, 6, 7, 9, 12, 13, 15, 20, 21),
    "mint_memphis_thesis_defense": (6, 9),
    "teal_illustrated_work_analysis": (5, 6, 7, 10, 13),
    "warm_bauhaus_quarterly_review": (6, 8, 9, 10, 14),
    "black_circuit_tech_launch": (4, 5, 10, 11, 13, 14, 15, 18),
    "green_aurora_tech_trends": (4, 5, 6, 8, 9, 10, 11, 14, 15, 16),
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
    "black_circuit_tech_launch": {13: 1},
}

# Every (label, card) pair the 54 reference pages write: the colour a run states, and the
# fill of the card that run sits inside -- the shape's own fill, or the smallest filled
# box that contains it, since a template routinely draws the card and the label as two
# shapes. Read off all 54 pages, commonest first, with the number of cards that write
# each pair as the last column -- a reply that has room for three names the three an
# author is most likely to clone, not the three with the lowest ratio, which measured
# are a 5%-alpha card and two accents on each other.
#
# The columns are the label's colour, the card's colour, the card's `lumMod`, `lumOff`
# and `alpha`, and how many cards on these pages are painted that way. A scheme name resolves to whatever the deck the page lands in
# paints that scheme with; a literal arrives unchanged, because nothing recolours a
# stated colour. That split is the whole mechanism this exists for: a page keeps its
# label and is given the deck's palette for its card, so a pair that reads in the
# template it was cut from can arrive unreadable, and the pairs are where it shows.
#
# The other three numbers are not decoration. A card is routinely a tint rather than the
# swatch -- 75/0 is the commonest card on these pages and 60/40 the next -- and 22 of the
# 89 measured failures landed on a tint and not on the colour the theme states. Alpha is
# here for the same reason in the other direction: a card at 15% is not its swatch, it is
# the page ground with a hint of the swatch in it, which is why `theme.unreadable_grounds`
# asks about both ends of the wash rather than picking one.
#
# Why the pairs and not a cross product of the colours they use: the first cut crossed
# eight inks against eight fills, asked about 80-odd pairs per deck and named a label in
# one accent on a card in another -- pairs no reference page makes, which crowded out the
# two that carry every failure. A second cut paired everything that shares a page and came
# back with 163 pairs no measured failure ever landed on. Pairing a label with the card it
# sits inside is what the renderer does, and it is done here, once, against files pinned
# by sha256 -- not at reply time, where resolving a stack of fills would reproduce the
# renderer that `measure.contrast` reads a render for.
#
# Calibrated against 486 clones of these pages into the ten bundled templates, rendered
# and read by `measure.contrast`: of the 89 pages that came back with copy under the
# ratio, 77 land on a colour these pairs name, in all ten hosts -- against 24 in one host
# for the `accent1`-against-white question this replaces. The 12 it does not name are
# cards painted by a shape behind the text rather than around it, which no pair can reach
# without the page.
BORROWED_LABEL_PAIRS: tuple[tuple[str, str, float, float, float, int], ...] = (
    ("#F8F8F8", "accent1", 0.75, 0.0, 1.0, 52),
    ("#FFFFFF", "accent1", 1.0, 0.0, 1.0, 45),
    ("#F8F8F8", "accent1", 1.0, 0.0, 1.0, 43),
    ("lt1", "accent1", 1.0, 0.0, 1.0, 23),
    ("#FFFFFF", "accent2", 0.6, 0.4, 1.0, 21),
    ("#FFFFFF", "accent1", 0.6, 0.4, 1.0, 19),
    ("lt1", "accent2", 1.0, 0.0, 1.0, 17),
    ("#FFFFFF", "accent2", 1.0, 0.0, 1.0, 17),
    ("lt1", "accent1", 0.75, 0.0, 1.0, 14),
    ("tx1", "accent1", 1.0, 0.0, 0.15, 13),
    ("lt1", "accent2", 0.6, 0.4, 1.0, 11),
    ("tx1", "bg1", 1.0, 0.0, 1.0, 8),
    ("tx1", "accent1", 1.0, 0.0, 1.0, 8),
    ("lt1", "accent3", 1.0, 0.0, 0.9, 8),
    ("bg1", "accent1", 1.0, 0.0, 1.0, 7),
    ("tx1", "accent1", 0.2, 0.8, 1.0, 6),
    ("tx1", "accent1", 1.0, 0.0, 0.5, 6),
    ("tx1", "accent2", 1.0, 0.0, 0.2, 6),
    ("tx1", "accent4", 0.2, 0.8, 1.0, 4),
    ("tx1", "accent1", 1.0, 0.0, 0.2, 4),
    ("accent1", "bg1", 1.0, 0.0, 1.0, 3),
    ("bg1", "accent2", 1.0, 0.0, 1.0, 2),
    ("accent2", "accent1", 1.0, 0.0, 0.2, 2),
    ("tx1", "accent1", 1.0, 0.0, 0.1, 2),
    ("tx1", "accent2", 1.0, 0.0, 0.1, 2),
    ("lt1", "accent1", 0.6, 0.4, 1.0, 1),
    ("#F8F8F8", "accent1", 1.0, 0.0, 0.05, 1),
    ("#F8F8F8", "accent1", 1.0, 0.0, 0.46, 1),
    ("accent4", "accent1", 1.0, 0.0, 0.15, 1),
)

# Under this, a borrowed page's label on a fill of that colour stops being type. The
# number is `measure.contrast.UNREADABLE_RATIO`, restated here rather than imported
# because this module must not depend on the measurement package; the two are checked
# against each other in the tests.
#
# It matters at borrowing time because it is the one thing a borrowed page cannot bring
# with it: the ink is stated on the run, not derived from the ground, so nothing
# recolours it. Measured over all 486 clones of these 54 pages into the ten bundled
# templates, rendered and read by `measure.contrast`: 89 pages came back with copy under
# this ratio, in every one of the ten hosts -- 28 in the one whose `accent1` renders
# white at 1.88:1, and 17 in the dark one, whose `accent1` reads 2.33:1 and whose
# `accent2` reads 1.91:1. Only 2 of the 54 pages read under the ratio in their own
# template's colours, and both are that template's own decoration, which the gate reports
# rather than refuses -- so a page that arrives unreadable arrived that way from the
# palette it landed in, and the palette is what `theme.unreadable_grounds` asks.
BORROWED_INK_READS = 2.0


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
        "Take one tagged dark only when the request asks for a dark, black or night look or a "
        "technology register; otherwise stay with the light ones. Do not invent a path or choose an "
        "irrelevant style:\n" + "\n".join(template.prompt_line() for template in available)
    )


def find_default_template(filename: str) -> DefaultTemplate | None:
    return next((template for template in default_template_catalog() if template.filename == filename), None)


def fallback_default_template() -> DefaultTemplate | None:
    available = default_template_catalog()
    return available[0] if available else None
