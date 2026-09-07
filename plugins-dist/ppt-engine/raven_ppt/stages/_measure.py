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
from raven_ppt.contracts.masters import Bands
from raven_ppt.services.gates import (
    DeckUnderReview,
    check_deck,
    figure_labels,
    load_figure_catalog,
)
from raven_ppt.services.gates.grading import checks_for
from raven_ppt.services.ingest import CATALOGUE_FILE, MATERIALS_FILE
from raven_ppt.services.template.bands import bands_of, read_bands, write_bands
from raven_ppt.stages._views import DeckViews


@dataclass
class DeckMeasurer:
    """Measure a built deck as thoroughly as this machine allows."""

    views: DeckViews = field(default_factory=DeckViews)
    skipped: list[str] = field(default_factory=list)

    async def __call__(
        self,
        project: Project,
        pptx: Path,
        outcome: BuildOutcome | None = None,
        changed: str = "delivery",
    ) -> list[Finding]:
        """Measure the built deck, running the checks the change is worth.

        `changed` says what this build altered, and the default is the one that runs
        everything: a caller that does not know spends the full pass rather than
        quietly skipping a refusal. See `gates.grading.checks_for` -- the names it
        leaves out are checks that did not run, which is not the same answer as a
        check that found nothing, and the caller has to carry the previous build's
        findings for them.
        """
        pdf = await self.views.pdf(pptx, project.review_dir)
        prototypes = _prototypes(project)
        deck = DeckUnderReview(
            pptx_path=pptx,
            pdf_path=pdf,
            rendered_pages=await self._rendered(project, pdf, outcome),
            outcome=outcome,
            figure_labels=_figure_labels(project),
            figure_catalogue=_figure_catalogue(project),
            materials=_materials(project),
            brief=load_brief(brief_path(project)),
            template=_template(project),
            prototypes=prototypes,
            band_grid=_bands(project, prototypes, pdf),
            outline=_outline(project),
        )
        self.skipped.clear()
        wanted = checks_for(changed, has_render=pdf is not None)
        return check_deck(deck, only=wanted, on_error=lambda name, exc: self.skipped.append(f"{name}: {exc}"))

    async def _rendered(self, project: Project, pdf: Path | None, outcome: BuildOutcome | None) -> list[Path] | None:
        """Every page as a PNG, rasterising only the pages that are not already one.

        Handed to the record rather than left to the checks. A pixel check that finds
        no renders rasterises the deck itself, at every page, on every build: measured
        on a fifteen-page 6.6MB deck, 19.6s of a 30.7s measurement, against 4.4s for
        the five pages a revision had touched. Passing them in also means one
        rasterisation serves every check instead of one per check.

        A page is reused only when its PNG is newer than the file it was drawn from and
        the deck still has the page count the directory holds. Both halves are needed:
        the PDF is rewritten whole on every build, so its own timestamp says nothing
        about which page changed, and a deck that gained or lost a page renumbers every
        PNG after the insertion.
        """
        if pdf is None:
            return None
        pages = getattr(outcome, "pages", None)
        if not isinstance(pages, int) or pages <= 0:
            return None
        folder = project.review_dir
        have = {number: folder / f"page-{number:03d}.png" for number in range(1, pages + 1)}
        stale = sorted(number for number, png in have.items() if not png.is_file())
        if len(list(folder.glob("page-*.png"))) != pages:
            stale = sorted(have)
        fresh = _drawn_again(project, outcome)
        if fresh is None:
            stale = sorted(have)
        else:
            stale = sorted(set(stale) | fresh)
        if stale:
            await self.views.pages_of(pdf, folder, stale)
        return [have[number] for number in sorted(have) if have[number].is_file()] or None


def _drawn_again(project: Project, outcome: BuildOutcome | None) -> set[int] | None:
    """The pages whose code differs from the version the last render was taken of.

    None when the question cannot be answered -- no fingerprints yet, or a page count
    that moved -- and the caller then rasterises everything, which is the safe way to
    be wrong about a cache.
    """
    from raven_ppt.backends.script.workspace import read_script
    from raven_ppt.services import seen

    sources = getattr(outcome, "sources", None)
    if not sources:
        return None
    try:
        script = read_script(project)
    except (OSError, AttributeError):
        return None
    blocks = seen.blocks_of(script, sources)
    if not blocks:
        return None
    # Every page's own code plus the prelude they all run: the pixels a page shows
    # depend on both, and a cache keyed on the page span alone served stale renders
    # to every pixel gate after a shared constant changed.
    prelude = seen.shared_digest(script, sources)
    blocks = {page: f"{digest}.{prelude}" for page, digest in blocks.items()}
    known = _rendered_marks(project)
    if known is None:
        _write_rendered_marks(project, blocks)
        return None
    _write_rendered_marks(project, blocks)
    return {page for page, digest in blocks.items() if known.get(str(page)) != digest}


RENDERED_FILE = "rendered.json"


def _rendered_marks(project: Project) -> dict[str, str] | None:
    """The fingerprints the PNGs on disk were taken of, or None when there are none."""
    import json

    try:
        raw = json.loads((project.state_dir / RENDERED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pages = raw.get("pages") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in pages.items()} if isinstance(pages, dict) else None


def _write_rendered_marks(project: Project, blocks: dict[int, str]) -> None:
    """Never fail a measurement over the cache's own bookkeeping."""
    import json

    try:
        project.state_dir.mkdir(parents=True, exist_ok=True)
        (project.state_dir / RENDERED_FILE).write_text(
            json.dumps({"pages": {str(page): digest for page, digest in blocks.items()}}, indent=1),
            encoding="utf-8",
        )
    except OSError:
        pass


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


def _bands(project: Project, prototypes: Path | None, pdf: Path | None) -> Bands | None:
    """The deck's band grid: the one recorded beside the template, else measured once.

    Written down on first measurement because deriving it opens the template and reads
    the render, and every build after the first would pay that again for an answer that
    cannot change while the template does not.
    """
    kept = read_bands(project)
    if kept is not None or prototypes is None:
        return kept
    measured = bands_of(prototypes, pdf)
    if measured is not None:
        try:
            write_bands(project, measured)
        except OSError:
            pass  # the reading still serves this build
    return measured


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
