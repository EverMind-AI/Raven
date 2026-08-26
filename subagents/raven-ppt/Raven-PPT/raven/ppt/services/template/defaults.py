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
        "white and royal blue with a flat black-and-yellow illustration; general-purpose analysis and recommendation decks, 11 example pages",
    ),
    DefaultTemplate(
        "blue_photo_work_summary.pptx",
        (
            "light",
            "corporate",
            "photographic",
            "blue",
            "summary",
            "report",
            "review",
            "business",
        ),
        "royal blue over an architectural photograph; corporate work summaries and period reviews, 20 example pages",
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
        "taupe_photo_business_plan.pptx",
        (
            "light",
            "editorial",
            "warm",
            "taupe",
            "photographic",
            "business-plan",
            "strategy",
            "pitch",
            "startup",
        ),
        "warm white and taupe over a restrained architectural photograph; business plans, pitches and strategy, 11 example pages",
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
        "cream and teal with a flat collaboration illustration; work analysis, process and planning decks, 11 example pages",
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
        "green_dimensional_design_portfolio.pptx",
        (
            "light",
            "creative",
            "green",
            "dimensional",
            "portfolio",
            "design",
            "advertising",
            "showcase",
        ),
        "green gradient with a rendered grass-textured wordmark; design portfolios and creative showcases, 18 example pages",
    ),
    DefaultTemplate(
        "green_playful_personal_resume.pptx",
        (
            "light",
            "playful",
            "creative",
            "green",
            "yellow",
            "resume",
            "personal",
            "profile",
        ),
        "yellow-to-green gradient with a rendered toy computer; personal resumes and profiles, 11 example pages",
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
        "vermilion with a line-drawn landscape and classical borders; traditional culture, heritage and history, 11 example pages",
    ),
)


def default_template_catalog() -> tuple[DefaultTemplate, ...]:
    return tuple(template for template in DEFAULT_TEMPLATES if template.path.is_file())


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
