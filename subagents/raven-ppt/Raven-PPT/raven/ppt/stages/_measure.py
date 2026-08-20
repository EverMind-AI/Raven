"""Assembling everything that can be known about a built deck, then checking it.

The gate registry takes a record and returns findings; this is what fills the
record in. Two things make that non-trivial and neither belongs in a service.
Rendering the deck is slow, blocking and optional -- a deck with no LibreOffice on
the machine can still be built, gated on its content and delivered, with the
render-truth checks reporting nothing rather than the call failing. And the fact
index and figure catalogue come off disk, which a stateless check has no business
reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from raven.ppt.contracts import BuildOutcome, Finding, Project, brief_path, load_brief
from raven.ppt.services.gates import (
    DeckUnderReview,
    check_deck,
    figure_labels,
    load_figure_catalog,
    load_source_index,
)
from raven.ppt.services.ingest import CATALOGUE_FILE, SOURCE_INDEX_FILE
from raven.ppt.stages._views import DeckViews


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
            source_index=_source_index(project),
            figure_labels=_figure_labels(project),
            brief=load_brief(brief_path(project)),
            template=_template(project),
            prototypes=_prototypes(project),
            outline=_outline(project),
        )
        self.skipped.clear()
        return check_deck(deck, on_error=lambda name, exc: self.skipped.append(f"{name}: {exc}"))


def _outline(project: Project):
    """The outline this deck promised to follow, when one was recorded."""
    from raven.ppt.contracts import load_outline, outline_path

    try:
        return load_outline(outline_path(project))
    except (OSError, ValueError):
        return None


def _source_index(project: Project):
    """The fact index, or None when nothing has been ingested.

    None means the fact gate reports nothing, which is the honest answer with no
    sources on record: there is nothing to anchor a claim against, and inventing a
    refusal would block every deck built without ingest.

    The file name comes from the ingest service rather than being written here.
    Passing the *directory* by mistake looked exactly like "nothing ingested" --
    the read raised, the read was caught, and a deck with an invented number in it
    published clean. The end-to-end test that caught it is in
    tests/ppt/test_end_to_end.py.
    """
    path = project.ingest_dir / SOURCE_INDEX_FILE
    if not path.is_file():
        return None
    try:
        return load_source_index(path)
    except (OSError, ValueError):
        return None


def _template(project: Project) -> Path | None:
    """The prepared template, when this deck has one.

    The prepared copy rather than the user's original, because that is the file the
    build was pointed at; the two share a theme, so either would answer the
    comparison, and naming the one the author was handed makes the finding
    actionable.
    """
    from raven.ppt.services.template import prepared_path

    path = prepared_path(project)
    return path if path.is_file() else None


def _prototypes(project: Project) -> Path | None:
    """The user's own template file, which is where the example pages still are."""
    from raven.ppt.services.template import template_path

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
