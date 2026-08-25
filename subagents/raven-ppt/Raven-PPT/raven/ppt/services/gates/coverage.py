"""Which checks could not run, said out loud.

Every gate in this registry is written so that a missing input means it reports
nothing. No fact index and the fact gate is silent; no brief and the page budget
and the language check are silent; no render and three of the measurements are
silent. Each of those is the right call on its own -- a deck built without sources
has no ground truth to be refused against, and inventing a refusal would block
every deck built without ingest -- and together they produce a reply in which
"checked and clean" and "never checked at all" are the same reply.

A run had exactly that shape: a directory was passed where a file was meant, the
read raised, the raise was caught, and a deck with an invented number in it
published with nothing to say about it. The path bug is fixed. The silence it hid
behind is what these close.

Four checks rather than one, so each is a row in the registry beside the checks it
stands in for, can be asked for alone, and has its own kind for a profile to
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


def unchecked_facts(deck) -> list[Finding]:
    if deck.source_index is not None:
        return []
    return [
        _gap(
            "unchecked_facts",
            "no sources were ingested, so no number, name or citation on any page has been checked against "
            "anything -- every figure in this deck is the author's word for it",
            "run ppt_ingest on the materials, or make it explicit on delivery that this deck was written "
            "without sources",
        )
    ]


def unchecked_citations(deck) -> list[Finding]:
    if deck.figure_labels:
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
