"""Where each part of `materials.md` starts, so it can be read a part at a time.

The file is the whole of the deck's evidence and it is long: a fourteen-page paper
comes out at 16k tokens. An author that reads it whole pays for it once and then
pays again on every request for the rest of the run -- measured on a live run, that
one `read_file` cost $8 of a $45 deck, because it entered the context at step 6 and
was re-sent 99 times.

Nothing here summarises. The headings `ingest` already writes ("# Source: paper.pdf",
"## [paper.pdf] page 3") are an index once they carry a line number, and a line
number is what `read_file(offset=, limit=)` takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Past this many parts the index costs more than it saves, and a hundred one-page
# entries is not something anyone reads. Beyond it only the sources are listed.
MAX_PARTS = 40


@dataclass(frozen=True)
class Section:
    """One heading in the materials, and the lines under it."""

    heading: str
    line: int
    """1-based, which is what `read_file`'s `offset` takes."""
    lines: int
    chars: int

    def entry(self) -> dict[str, object]:
        return {"heading": self.heading, "line": self.line, "lines": self.lines, "chars": self.chars}


def sections(path: Path, *, depth: int = 2) -> tuple[Section, ...]:
    """Every heading of `depth` or shallower, with the span under it."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ()
    lines = text.splitlines()
    found: list[tuple[str, int]] = []
    for number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= level <= depth:
            found.append((stripped[level:].strip(), number))
    if not found:
        return ()
    out: list[Section] = []
    for index, (heading, start) in enumerate(found):
        end = found[index + 1][1] - 1 if index + 1 < len(found) else len(lines)
        body = "\n".join(lines[start - 1 : end])
        out.append(Section(heading=heading, line=start, lines=end - start + 1, chars=len(body)))
    return tuple(out)


def index(path: Path) -> list[dict[str, object]]:
    """The index as a tool returns it: every part, or just the sources if there are
    too many to list."""
    parts = sections(path)
    if len(parts) <= MAX_PARTS:
        return [part.entry() for part in parts]
    return [part.entry() for part in sections(path, depth=1)]
