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
import re
from dataclasses import dataclass, field
from pathlib import Path

from raven_ppt.contracts.project import Project

SCHEMA = "raven_ppt.outline.v1"
OUTLINE_FILE = "outline.json"


def outline_path(project: Project) -> Path:
    return project.state_dir / OUTLINE_FILE


_ID = re.compile(r"\b([PM])(\d+)\b", re.IGNORECASE)


def layout_fields(entry: dict) -> dict[str, object]:
    """`layout`, `layers` and `anti_pattern` off one entry, old shape or new.

    The old shape wrote every id into `layout` as `"P14 + M4 + M11"`, and outlines in
    that shape are on disk. Read here rather than at the tool's edge so a plan loaded
    from a file and a plan just submitted come out the same, which is the property the
    section field lost by being written and never read back.
    """
    written = str(entry.get("layout") or "").strip()
    found = [(kind.upper(), number) for kind, number in _ID.findall(written)]
    structure = next((f"P{number}" for kind, number in found if kind == "P"), written if not found else "")
    layers = [f"M{number}" for kind, number in found if kind == "M"]
    layers += [str(one).strip().upper() for one in entry.get("layers") or () if str(one).strip()]
    return {
        "layout": structure,
        # Deduplicated in the order they were named: the same modifier written into both
        # halves of a half-migrated entry is one layer on the page, not two.
        "layers": tuple(dict.fromkeys(layers)),
        "anti_pattern": str(entry.get("anti_pattern") or "").strip(),
    }


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
    """Which page structure this page is composed on, as one id.

    `"P14"`, from `references/layouts.md`, whose Part 1 is eleven skeletons with every id
    folded into the one it varies. One id and not a sum: the structure is the page's
    bones and the modifiers stack on it, so they are `layers` and reading this column
    down the deck is what says whether a range of structures was used. Structured because
    a page's shape was decided while its geometry was being typed, and what came of that
    is measurable -- one delivered deck drew its own layout on eleven pages, four of
    which are the same eight lines (a table, one rounded plane, three points).

    Empty says "this page is a template clone, or I have not decided"; an id the
    catalogue does not carry is refused, because it says something false for free."""
    layers: tuple[str, ...] = field(default_factory=tuple)
    """The modifier layers stacked on that structure, by id: `("M4", "M11")`.

    Its own field because it was written into `layout` as `"P14 + M4 + M11"` and a
    string of ids cannot be a closed list -- so the one field that decides a page's shape
    took free text, and a live run filled it with `P01`, `P04`, `P07`, none of which is
    an id. Split, both halves are enumerable at the tool's edge."""
    anti_pattern: str = ""
    """What this page must not turn into, in a line.

    The structure says what the page is; this says which way it goes wrong -- "two
    columns of unrelated bullets with a picture in one of them", "six cells filled
    because there are six cells". Part 1's third column carries one per skeleton and
    this is where the page's own goes, written before the geometry, because by the time
    the render shows it the page has been drawn."""
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
    borrowed: str = ""
    """The bundled template `prototype` is numbered in, when it is not the bound one.

    Empty for a page built on the bound template's own example. A stem such as
    `gold_panel_year_end_summary` says the page was cloned out of that template with
    `prototype(bundled(stem), n)`: its colours and master follow the deck, so what
    it borrows is the arrangement. Recorded so the checks that read the plan --
    which file a page promised, whose placeholder copy and photographs to look for,
    which pages are furniture -- open the right file."""

    def as_dict(self) -> dict[str, object]:
        return {
            "page": self.page,
            "claim": self.claim,
            "carries": self.carries,
            "layout": self.layout,
            "layers": list(self.layers),
            "anti_pattern": self.anti_pattern,
            "figures": list(self.figures),
            "says": list(self.says),
            "section": self.section,
            "needs": self.needs,
            "prototype": self.prototype,
            "borrowed": self.borrowed,
        }

    def summary(self) -> str:
        said = f"{self.page}. {self.claim}"
        if self.carries:
            said += f"  [{self.carries}]"
        if self.prototype is not None and self.borrowed:
            said += f"  (from {self.borrowed} page {self.prototype})"
        elif self.prototype is not None:
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
                    **layout_fields(entry),
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
                    borrowed=str(entry.get("borrowed") or "").strip(),
                )
            )
        except (TypeError, ValueError):
            continue
    return Outline(takeaway=str(raw.get("takeaway", "")), pages=tuple(pages))
