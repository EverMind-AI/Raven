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
"""

from __future__ import annotations

from raven.ppt.contracts import Audience, Finding, Severity

# What the render is the only way to measure. Named in the finding, because "the
# deck could not be rendered" is a fact about the machine and "nothing checked
# what a reader will actually see" is the consequence.
_RENDER_DEPENDENT = ("word collision", "rule strike-through", "card overflow")


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
    if deck.pdf_path is not None:
        return []
    return [
        _gap(
            "unrendered",
            "the deck could not be rendered on this machine, so nothing measured on the rendered page ran: "
            + ", ".join(_RENDER_DEPENDENT)
            + ". Only the geometry the file declares was checked",
            "install LibreOffice to measure what a reader will actually see",
        )
    ]


def _gap(kind: str, problem: str, remedy: str) -> Finding:
    return Finding(kind=kind, severity=Severity.WARNING, audience=Audience.AUTHOR, message=f"{problem}. {remedy}")
