"""Where each part of `materials.md` starts, so it can be read a part at a time.

The file is the whole of the deck's evidence and it is long: a fourteen-page paper
comes out at 16k tokens. An author that reads it whole pays for it once and then
pays again on every request for the rest of the run -- measured on a live run, that
one `read_file` cost $8 of a $45 deck, because it entered the context at step 6 and
was re-sent 99 times.

The headings `ingest` writes ("# Source: paper.pdf", "## [paper.pdf] page 3") carry
a line number, and a line number is what `read_file(offset=, limit=)` takes. But a
page number says nothing about what is on the page: measured against a run without
the index, an author holding one read *more* of the file, not less -- 65% against
37% -- because a list of page numbers is a checklist, not a filter. So each part
also carries its opening line.

That line is there to rule parts out, never to pick them: it is enough to recognise
a bibliography or a table of class names, and it is not enough to judge whether a
paragraph matters. Ninety characters cannot stand in for five thousand, and a part
whose first line reads flat may still hold the one number the deck needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Past this many parts the index costs more than it saves, and a hundred one-page
# entries is not something anyone reads. Beyond it only the sources are listed.
MAX_PARTS = 40

# Long enough to tell "References" and "Table 4. Ablation results ..." apart, short
# enough that forty of them cost less than one page of the file they save reading.
GIST_CHARS = 90


@dataclass(frozen=True)
class Section:
    """One heading in the materials, and the lines under it."""

    heading: str
    line: int
    """1-based, which is what `read_file`'s `offset` takes."""
    lines: int
    chars: int
    gist: str
    """The part's first line of real text -- enough to recognise a bibliography."""

    def entry(self) -> dict[str, object]:
        return {
            "heading": self.heading,
            "line": self.line,
            "lines": self.lines,
            "chars": self.chars,
            "gist": self.gist,
        }


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
        out.append(
            Section(
                heading=heading,
                line=start,
                lines=end - start + 1,
                chars=len(body),
                gist=_gist(lines[start:end]),
            )
        )
    return tuple(out)


def _gist(body: list[str]) -> str:
    """The first line under a heading that a reader would call text.

    Blank lines and rules are skipped; a page of figure labels has none of those and
    yields its first label, which is the honest answer -- that part is fragments.
    """
    for line in body:
        said = line.strip().lstrip("#").strip()
        if said and said.strip("-=_*|· "):
            return said[:GIST_CHARS]
    return ""


def index(path: Path) -> list[dict[str, object]]:
    """The index as a tool returns it: every part, or just the sources if there are
    too many to list."""
    parts = sections(path)
    if len(parts) <= MAX_PARTS:
        return [part.entry() for part in parts]
    return [part.entry() for part in sections(path, depth=1)]


def stated_chars(text: str) -> int:
    """How much text the material itself carries, ingest's own anchors excluded.

    The headings this pipeline writes ("## [paper.pdf] page 3") are structure, not
    evidence, so a deck's page budget must not be justified by them: forty page
    anchors would otherwise read as a document with something to say.
    """
    total = 0
    lines = text.splitlines()
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        total += len(line.strip())
    return total
