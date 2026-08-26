"""What the deck is for, as agreed with the person asking for it.

Three questions the author cannot answer from the materials, because the answer
is not in them: what language the audience reads, who the audience is and on what
occasion, and how long the talk is. A paper is written in English and presented in
Chinese; the same results become a fifteen-minute conference talk or a
five-minute internal update; and "16-20 slides" is a constraint from outside the
paper entirely.

The reason they are a contract rather than three prompt sentences: an agreed
decision that binds nothing is prose. The page budget is checked against the built
deck, the language is checked against what the pages actually say, and what the user
ruled out is quoted back on every build. Anything here that could
not be checked has no business being confirmed with a user -- it would ask them to
decide something and then ignore it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from raven.ppt.contracts.project import Project

SCHEMA = "raven.ppt.brief.v1"
BRIEF_FILE = "brief.json"


def brief_path(project: Project) -> Path:
    """Where a project keeps what was agreed. One answer, so nothing can look
    for it in the wrong place -- which is how a check once read a directory
    where a file was meant and reported "nothing was ingested"."""
    return project.state_dir / BRIEF_FILE


@dataclass(frozen=True)
class PageBudget:
    """How many slides the talk has room for.

    A range rather than a number, because the honest answer to "how many slides"
    is a span: a fifteen-minute talk is twelve to eighteen pages depending on how
    much of it is a figure.
    """

    low: int
    high: int

    def __post_init__(self) -> None:
        if self.low < 1:
            raise ValueError("a deck has at least one page")
        if self.high < self.low:
            raise ValueError(f"page budget {self.low}..{self.high} runs backwards")

    def holds(self, pages: int) -> bool:
        return self.low <= pages <= self.high

    def __str__(self) -> str:
        return f"{self.low}" if self.low == self.high else f"{self.low}-{self.high}"


@dataclass(frozen=True)
class DeckBrief:
    """The three answers, and whatever else the user said."""

    language: str
    audience: str
    pages: PageBudget
    notes: tuple[str, ...] = field(default_factory=tuple)
    forbidden: tuple[str, ...] = field(default_factory=tuple)
    """What this deck may not use, one thing each: "no icons", "no comparison
    tables", "never name a competitor", "no dark pages".

    Here rather than on the outline because of what it has to outlive. A prohibition
    is agreed once, before anything is drawn, and it still holds after the argument
    is replanned -- on the outline it would be rewritten by the next `ppt_outline`
    call and the deck would quietly regain the thing the user ruled out. It is not a
    property of one page either: "no icons" is a statement about the deck.

    Structured rather than left inside `notes`, because of how it binds. A rule
    agreed once and never said again is a rule nothing is holding: this route has
    already shipped that mistake, in the icon and theme summaries written for
    "callers who cannot see the catalogue" and then never injected anywhere. So
    `ppt_build` quotes these lines back on every build, which is the same way
    `audience` binds -- by reaching the call that would otherwise decide without it.
    """

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("a brief needs the language the audience reads")
        if not self.audience.strip():
            raise ValueError("a brief needs to say who the deck is for")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "language": self.language,
            "audience": self.audience,
            "pages": {"low": self.pages.low, "high": self.pages.high},
            "notes": list(self.notes),
            "forbidden": list(self.forbidden),
        }

    def summary(self) -> str:
        """One line, for a prompt that has to carry the brief without a schema."""
        said = f"For {self.audience}, in {self.language}, {self.pages} slides."
        if self.forbidden:
            said += " Not to be used: " + "; ".join(self.forbidden) + "."
        return said + ("" if not self.notes else " " + " ".join(self.notes))


def write_brief(brief: DeckBrief, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")


def load_brief(path: Path) -> DeckBrief | None:
    """The agreed brief, or None when none was recorded.

    None is a real state: a run that was never asked has no brief, and the checks
    that depend on one report nothing rather than inventing a budget to fail
    against.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pages = raw.get("pages") or {}
    try:
        return DeckBrief(
            language=str(raw.get("language", "")),
            audience=str(raw.get("audience", "")),
            pages=PageBudget(low=int(pages.get("low", 1)), high=int(pages.get("high", 1))),
            notes=tuple(str(note) for note in (raw.get("notes") or [])),
            forbidden=tuple(str(item) for item in (raw.get("forbidden") or [])),
        )
    except (TypeError, ValueError):
        return None
