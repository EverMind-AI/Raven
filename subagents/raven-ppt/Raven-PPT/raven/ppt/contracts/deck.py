"""The outline: what each page is for, before anything is drawn.

Two of the three routes need this and one does not. The script route's plan
*is* the program the author wrote, so it produces a DeckPlan only as a record
of intent; the slot and image-text routes consume it as the input to their
per-page work. Keeping it in contracts rather than in either route's package is
what lets the outline gate be written once.

Note what is absent: no coordinates, no sizes, no font, no colour. A page here
says what it is about and what evidence it stands on. Everything about how it
looks belongs to a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PageSpec:
    """One page's brief."""

    number: int
    section: str
    headline: str
    intent: str = ""
    # Figure ids from ingest that this page stands on. The citation gate reads
    # this against what the finished page actually shows.
    figures: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("pages are numbered from 1")
        if not self.headline.strip():
            raise ValueError(f"page {self.number} needs a headline")


@dataclass(frozen=True)
class DeckPlan:
    """The whole deck's brief, in order."""

    title: str
    pages: tuple[PageSpec, ...]

    def __post_init__(self) -> None:
        numbers = [p.number for p in self.pages]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"pages must run 1..{len(numbers)} without gaps, got {numbers}")

    def page(self, number: int) -> PageSpec | None:
        return next((p for p in self.pages if p.number == number), None)
