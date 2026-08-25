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
        "5407055_green_fresh_summary.pptx",
        (
            "fallback",
            "default",
            "light",
            "fresh",
            "green",
            "friendly",
            "editorial",
            "general-purpose",
            "technology",
            "software",
            "product",
            "platform",
            "comparison",
            "competitive-analysis",
            "strategy",
            "research",
            "data",
        ),
        "white and soft green with restrained editorial accents; the general fallback for product, platform, technology, research, comparison and strategy decks",
    ),
    DefaultTemplate("5407011_brown_minimal_business_plan.pptx", ("light", "editorial", "business-plan", "warm"), "warm white and taupe, restrained editorial cover, good for strategy and business plans"),
    DefaultTemplate("5407012_blue_business_analysis.pptx", ("light", "business", "analysis", "comparison", "blue"), "white and royal blue, clear business-analysis hierarchy, good for comparison decks"),
    DefaultTemplate("5407019_blue_minimal_medical.pptx", ("light", "minimal", "medical", "clean", "blue"), "white and blue with generous margins, calm and legible for technical or medical topics"),
    DefaultTemplate("5407044_blue_business_summary.pptx", ("light", "business", "summary", "report", "blue"), "white, pale blue and photographic cover, suitable for reports and operational reviews"),
    DefaultTemplate("5407199_blue_business_product.pptx", ("light", "business", "product", "corporate", "blue"), "white and blue with architectural product imagery, suitable for product and platform decks"),
    DefaultTemplate("5407053_blue_minimal_analysis.pptx", ("light", "minimal", "analysis", "general", "blue"), "white and blue with abstract geometry, suitable for general analysis and strategy decks"),
    DefaultTemplate("5407041_green_minimal_practice.pptx", ("light", "minimal", "practice", "general", "green"), "light green and black with simple geometry, suitable for general project and planning decks"),
    DefaultTemplate("5407566_green_business_medical.pptx", ("light", "medical", "business", "report", "green"), "warm white and muted green, structured report layout, suitable for evidence-heavy decks"),
    DefaultTemplate("5407617_blue_minimal_general_analysis.pptx", ("light", "minimal", "analysis", "general", "blue"), "white and blue, general-purpose analysis layout with strong content-page rhythm"),
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
