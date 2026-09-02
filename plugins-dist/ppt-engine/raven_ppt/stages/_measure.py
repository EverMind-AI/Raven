"""Assembling everything that can be known about a built deck, then checking it.

The gate registry takes a record and returns findings; this is what fills the
record in. Two things make that non-trivial and neither belongs in a service.
Rendering the deck is slow, blocking and optional -- a deck with no LibreOffice on
the machine can still be built, gated on its content and delivered, with the
render-truth checks reporting nothing rather than the call failing. And the brief,
the outline and the figure catalogue come off disk, which a stateless check has no
business reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from raven_ppt.contracts import BuildOutcome, Finding, Project, brief_path, load_brief
from raven_ppt.services.gates import (
    DeckUnderReview,
    check_deck,
    figure_labels,
    load_figure_catalog,
)
from raven_ppt.services.ingest import CATALOGUE_FILE, MATERIALS_FILE
from raven_ppt.stages._views import DeckViews


@dataclass
class DeckMeasurer:
    """Measure a built deck as thoroughly as this machine allows."""

    views: DeckViews = field(default_factory=DeckViews)
    skipped: list[str] = field(default_factory=list)

    async def __call__(self, project: Project, pptx: Path, outcome: BuildOutcome | None = None) -> list[Finding]:
        pdf = await self.views.pdf(pptx, project.review_dir)
        deck = DeckUnderReview(
            pptx_path=pptx,
            pdf_path=pdf,
            outcome=outcome,
            figure_labels=_figure_labels(project),
            figure_catalogue=_figure_catalogue(project),
            materials=_materials(project),
            brief=load_brief(brief_path(project)),
            template=_template(project),
            prototypes=_prototypes(project),
            outline=_outline(project),
        )
        self.skipped.clear()
        return check_deck(deck, on_error=lambda name, exc: self.skipped.append(f"{name}: {exc}"))


def _outline(project: Project):
    """The outline this deck promised to follow, when one was recorded."""
    from raven_ppt.contracts import load_outline, outline_path

    try:
        return load_outline(outline_path(project))
    except (OSError, ValueError):
        return None


def _template(project: Project) -> Path | None:
    """The prepared template, when this deck has one.

    The prepared copy rather than the user's original, because that is the file the
    build was pointed at; the two share a theme, so either would answer the
    comparison, and naming the one the author was handed makes the finding
    actionable.
    """
    from raven_ppt.services.template import prepared_path

    path = prepared_path(project)
    return path if path.is_file() else None


def _prototypes(project: Project) -> Path | None:
    """The user's own template file, which is where the example pages still are."""
    from raven_ppt.services.template import template_path

    path = template_path(project)
    return path if path is not None and path.is_file() else None


def _figure_labels(project: Project):
    """sha256 of each ingested figure -> the label its own source gave it.

    The catalogue is keyed by figure id and the gate looks figures up by the bytes
    on the page, so the mapping has to be built here. Passing the catalogue itself
    type-checked -- both are `Mapping[str, ...]` -- and every lookup missed, so the
    citation gate stayed silent while `unchecked_citations` stayed quiet too: a
    non-empty catalogue reads as "citations were checked".
    """
    path = project.ingest_dir / CATALOGUE_FILE
    if not path.is_file():
        return None
    try:
        return figure_labels(project.figures_dir, load_figure_catalog(path)) or None
    except (OSError, ValueError):
        return None


def _materials(project: Project) -> str:
    """Everything this deck was given to read, or "" when nothing was ingested."""
    try:
        return (project.ingest_dir / MATERIALS_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def _figure_catalogue(project: Project):
    path = project.ingest_dir / CATALOGUE_FILE
    if not path.is_file():
        return None
    try:
        return load_figure_catalog(path)
    except (OSError, ValueError):
        return None
