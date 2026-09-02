"""What the deck argues, page by page, before a line of it is drawn.

The route had no such stage and it showed. An author went from the materials
straight to a python-pptx program, so what each page said was decided while its
geometry was being typed -- and the decks that came out were thin: eight pages
carrying a title and three short lines each, with nothing having ever asked what
the audience has to believe by the end.

Deciding that is the deck's one genuinely creative act, so it belongs to the author
rather than to a pass that runs from code. What belongs here is the part that can be
checked, and two things can:

A figure the plan means to place has to exist in the catalogue. And the page count
meets the brief's budget now, rather than after eighteen pages of program have been
written against a budget of ten. What a page *says* is not checked against anything:
the gate that held a number in the outline against an index of the materials was
deleted with that index (design doc D3a).

The fourth thing it does is not a check: a page that names what it still needs turns
into a search. This is the moment when what the deck is missing is actually known --
`ppt_prepare` has to guess it before anything knows what the pages are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from raven_ppt.contracts.project import Project

SCHEMA = "raven_ppt.outline.v1"
OUTLINE_FILE = "outline.json"


def outline_path(project: Project) -> Path:
    return project.state_dir / OUTLINE_FILE


@dataclass(frozen=True)
class PagePlan:
    """One page, as an argument rather than as a layout."""

    page: int
    claim: str
    """What this page says, as a statement. "Results" is a topic; "One model
    matches four task-specific ones" is a claim, and it is also the title."""
    carries: str = ""
    """What carries it: a figure id, a table, a chart, a number, a diagram."""
    layout: str = ""
    """Which page structure and modifier layers this page is composed of, by id.

    `"P14 + M4 + M11"`, from `references/layouts.md`. Structured because a page's shape
    was decided while its geometry was being typed, and
    what came of that is measurable -- one delivered deck drew its own layout on eleven
    pages, four of which are the same eight lines (a table, one rounded plane, three
    points). Declared here, the choice is reviewable before anything is drawn, and the
    deck's spread of structures is legible by reading down the column."""
    figures: tuple[str, ...] = field(default_factory=tuple)
    says: tuple[str, ...] = field(default_factory=tuple)
    """The supporting points, in the deck's language.

    Read by the replanner and measured for thinness, and checked against nothing else:
    a number written here reaches the page on the author's word alone."""
    section: str = ""
    """Which movement of the deck this page belongs to, named for this material.

    Additive rather than a level of its own: every human outline read for this --
    238 of them across five domains -- is a list of *sections* with one or more
    slides each, and the section names are the material's own argument, not a
    template. An academic paper's came out as background / limitations of existing
    work / the method / setup / results; an earnings release's as leadership
    context / financial deep dive / segment performance / closing and disclaimers;
    a lecture's as the five realities the textbook itself names; a speech's as the
    four arguments it makes. Pages sharing a name are one movement, and a deck
    whose pages have no movement between them is the flat list this is here to
    stop.
    """

    needs: str = ""
    """What the page lacks and the materials do not have. Becomes a search."""
    prototype: int | None = None
    """Which of the template's example pages this page adapts, when one is bound.

    Here rather than left to the program because of what happened without it: the
    author planned twelve pages as arguments, then wrote geometry for all twelve from
    scratch on the emptiest layout the template had, and the template survived as a
    background colour. Deciding "this is the metric row, page 5 of the template is
    the metric row" belongs with deciding what the page says -- by the time the
    program is being typed, inventing a layout is the path of least resistance.

    `None` means drawn from scratch, which is a legitimate answer for a page the
    template has no page for; `needs` is where the reason goes."""

    def as_dict(self) -> dict[str, object]:
        return {
            "page": self.page,
            "claim": self.claim,
            "carries": self.carries,
            "layout": self.layout,
            "figures": list(self.figures),
            "says": list(self.says),
            "section": self.section,
            "needs": self.needs,
            "prototype": self.prototype,
        }

    def summary(self) -> str:
        said = f"{self.page}. {self.claim}"
        if self.carries:
            said += f"  [{self.carries}]"
        if self.prototype is not None:
            said += f"  (from template page {self.prototype})"
        return said


@dataclass(frozen=True)
class Outline:
    """The whole argument, and what the audience is meant to leave with."""

    takeaway: str
    pages: tuple[PagePlan, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {"schema": SCHEMA, "takeaway": self.takeaway, "pages": [p.as_dict() for p in self.pages]}

    def summary(self) -> str:
        """The outline as the author reads it while writing the program."""
        return "\n".join((f"The deck argues: {self.takeaway}", *(page.summary() for page in self.pages)))

    @property
    def figures(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for page in self.pages:
            for figure in page.figures:
                seen.setdefault(figure, None)
        return tuple(seen)


def write_outline(outline: Outline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outline.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def load_outline(path: Path) -> Outline | None:
    """The recorded outline, or None when there is none or it cannot be read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    pages = []
    for entry in raw.get("pages") or ():
        if not isinstance(entry, dict):
            continue
        try:
            pages.append(
                PagePlan(
                    page=int(entry.get("page", 0)),
                    claim=str(entry.get("claim", "")),
                    carries=str(entry.get("carries") or ""),
                    layout=str(entry.get("layout") or ""),
                    figures=tuple(str(f) for f in entry.get("figures") or ()),
                    says=tuple(str(s) for s in entry.get("says") or ()),
                    needs=str(entry.get("needs") or ""),
                    # Written by `as_dict` since it was added and never read back: a plan
                    # recorded its section on every call and the build stage saw "" on
                    # every one, so the one field that says which movement a page belongs
                    # to reached nothing. Any field added above without a line here goes
                    # the same way silently.
                    section=str(entry.get("section") or ""),
                    prototype=int(entry["prototype"]) if entry.get("prototype") is not None else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return Outline(takeaway=str(raw.get("takeaway", "")), pages=tuple(pages))
