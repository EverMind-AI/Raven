"""What a backend hands back, whatever the backend was.

The three backends have nothing in common internally -- one runs a program the
model wrote, one compiles slot geometry through the vendored SVG converter, one
composites type over a generated image -- and that is the point of this type:
downstream, measurement, the gates and publication see one shape and never
learn which route produced the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PageSource:
    """Which part of the author's input produced one page.

    Review works one page at a time, so a render has to be matched to the code or
    the spec that drew that page and nothing else. Reading it from an author-written
    marker made annotation the model's job and let a stale number pair one
    page's render with another page's code; the script backend derives it from
    execution instead, and records the script digest so a mapping can never be
    read against a file it does not describe.
    """

    page: int
    first_line: int
    last_line: int


@dataclass(frozen=True)
class BuildOutcome:
    """The result of composing a deck. `ok` false means no usable file."""

    ok: bool
    pptx_path: Path | None = None
    pages: int = 0
    stdout: str = ""
    stderr: str = ""
    # Something the harness did on the author's behalf, kept apart from stderr
    # so tool output never blurs the two.
    note: str = ""
    sources: tuple[PageSource, ...] = field(default_factory=tuple)
    source_digest: str = ""

    def source_for(self, page: int) -> PageSource | None:
        return next((s for s in self.sources if s.page == page), None)
