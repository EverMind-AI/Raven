"""What a gate or a measurement found, in the one shape the whole system uses.

The predecessor returned six different shapes for this -- `fact_violations`,
`colour_bars`, `content_load`, `page_defects`, `undersized_type`,
`pages_not_in_build_py` -- each with its own keys, its own severity convention
(some refused the deck, some only warned, and which was which lived in the
caller). Every consumer had to know all six. One type with two explicit fields
replaces that: how bad it is, and a sentence that says what to do.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class Severity(Enum):
    """Whether a finding stops the deck.

    BLOCKING is fail-closed: the deck is not published while it stands. It is
    reserved for what a page credits -- a figure cited as another figure -- for
    what the deck was agreed to be, and for a deck the pipeline cannot reason
    about at all. A route may also declare a WARNING kind fatal for itself; see
    `Profile.blocking_kinds`.

    WARNING rides along with the deck and is reported to the author.
    Measurements of the *rendered page* are warnings by design: type
    size, overflow and overlap are all satisfiable by shrinking the copy, so a
    gate that refused publication until they cleared could be answered by
    making the page worse, and the fix loop oscillates instead of converging.
    """

    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One problem, on one page, and what to do about it."""

    kind: str
    severity: Severity
    message: str
    page: int | None = None
    detail: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("a finding needs a kind")
        if not self.message:
            raise ValueError(f"{self.kind} finding needs a message the model can act on")
        # Frozen protects the binding, not the mapping behind it.
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))


def blocking(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity is Severity.BLOCKING]


def warnings(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity is Severity.WARNING]
