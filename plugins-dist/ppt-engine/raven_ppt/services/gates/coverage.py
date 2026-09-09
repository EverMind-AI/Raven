"""Which checks could not run, said out loud.

Every gate in this registry is written so that a missing input means it reports
nothing. No brief and the page budget and the language check are silent; no
figure labels and the citation check is silent; no render and three of the
measurements are silent. Each of those is the right call on its own, and
together they produce a reply in which "checked and clean" and "never checked at
all" are the same reply. These close that gap.

Three checks rather than one, so each is a row in the registry beside the checks
it stands in for, can be asked for alone, and has its own kind for a profile to
declare on. Warnings, all of them: an absent input is not a defect in the deck,
and refusing on one would be the refusal the other gates correctly decline to
invent. What they change is what the reply -- and the report written beside the
published file -- is able to claim.

`unrendered` answers two questions rather than one, because "no PDF came back"
has two causes and they take opposite remedies. A machine with no LibreOffice is
a deployment fact and installing it is the fix; a machine that has one and a deck
that still has no PDF is a fact about the deck, and telling that author to
install a renderer sends them at the one thing that is already there. The probe
that tells them apart is the same one the converter itself uses.

And a render that came back is not automatically a render that can be measured. A
page image that is blank, empty or the wrong size has none of what the pixel
measurements look for, so all of them pass and the page reads as clean; the sanity
pass in `render.unusable_pages` is what turns that silence into a report. It runs
over the page images this review already has -- the ones handed over on the record,
else the ones the contrast measurement wrote beside the PDF earlier in this same
registry pass -- so it costs no second rasterisation, and reports nothing when
there are none to look at.
"""

from __future__ import annotations

from pathlib import Path

from raven_ppt.contracts import Finding, Severity
from raven_ppt.services.render.capabilities import available
from raven_ppt.services.render.errors import RenderError, RenderUnavailableError
from raven_ppt.services.render.pdf import page_sizes, unusable_pages

# What the render is the only way to measure. Named in the finding, because "the
# deck could not be rendered" is a fact about the machine and "nothing checked
# what a reader will actually see" is the consequence.
_RENDER_DEPENDENT = ("word collision", "rule strike-through", "card overflow")
# And what only the page's pixels can answer, as against the word boxes above:
# both contrast rows read the rendered image, so a page whose image did not come
# out has been measured for neither, however complete the rest of the render is.
_PIXEL_DEPENDENT = ("type a reader cannot make out", "type that is legible but thin")
# What `render.PAGE_PNG` writes, and therefore every page image a render has left
# beside the deck it came from.
_PAGE_IMAGES = "page-*.png"


def unchecked_citations(deck) -> list[Finding]:
    if deck.figure_labels or deck.figure_catalogue is not None:
        return []
    return [
        _gap(
            "unchecked_citations",
            "no figure catalogue was ingested, so a page citing Figure 4 while showing Figure 5 would not "
            "have been caught",
            "run ppt_ingest, which extracts the figures and the label each one carries in its own source",
        )
    ]


def unchecked_agreement(deck) -> list[Finding]:
    if deck.brief is not None:
        return []
    return [
        _gap(
            "unchecked_agreement",
            "no brief was recorded, so neither the length of this deck nor the language it is written in has "
            "been checked against anything that was agreed",
            "record what the user asked for with ppt_brief",
        )
    ]


def unrendered(deck) -> list[Finding]:
    if deck.pdf_path is None or not Path(deck.pdf_path).is_file():
        return [_no_pdf()]
    return _unusable_pages(deck)


def _no_pdf() -> Finding:
    """No PDF, said as the cause it actually has.

    `can_convert` is the converter's own precondition -- `office.to_pdf` raises
    `RenderUnavailableError` on exactly the same answer -- so the two branches
    here are the two errors it distinguishes, recovered after the stage that
    swallowed them turned both into None.
    """
    silent = ", ".join(_RENDER_DEPENDENT)
    if not available().can_convert:
        return _gap(
            "unrendered",
            "the deck could not be rendered on this machine, so nothing measured on the rendered page ran: "
            + silent
            + ". Only the geometry the file declares was checked",
            "install LibreOffice to measure what a reader will actually see",
        )
    return _gap(
        "unrendered",
        "LibreOffice is installed on this machine and this deck still has no PDF, so nothing measured on the "
        "rendered page ran: " + silent + ". Only the geometry the file declares was checked",
        "the renderer is not what is missing, so this file is where to look: convert it again and read what "
        "LibreOffice reports about it, because installing anything will not change it",
    )


def _unusable_pages(deck) -> list[Finding]:
    pdf = Path(deck.pdf_path)
    try:
        sizes = page_sizes(pdf)
    except RenderUnavailableError:
        # No reader on the machine, so there is no record to check an image
        # against. A capability gap rather than anything about this deck.
        return []
    except RenderError as exc:
        return [
            _gap(
                "unrendered",
                f"the deck converted but the PDF it produced cannot be read back ({exc}), so nothing measured "
                "on the rendered page ran: " + ", ".join(_RENDER_DEPENDENT),
                "convert the deck again -- a PDF no reader will open is not a render, however it exited",
            )
        ]
    pngs = list(deck.rendered_pages) if deck.rendered_pages is not None else sorted(pdf.parent.glob(_PAGE_IMAGES))
    if not pngs:
        return []
    return [
        _gap(
            "unrendered",
            f"page {number} did not come out of the renderer: {reason}, so nothing measured off the page image "
            "ran on it: " + ", ".join(_PIXEL_DEPENDENT),
            f"render the deck again and look at page {number} -- until its image comes out, a clean report for "
            "that page is a report on nothing",
            page=number,
        )
        for number, reason in unusable_pages(sizes, pngs).items()
    ]


def _gap(kind: str, problem: str, remedy: str, page: int | None = None) -> Finding:
    return Finding(kind=kind, severity=Severity.WARNING, message=f"{problem}. {remedy}", page=page)
