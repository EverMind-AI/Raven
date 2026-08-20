"""Which part of the program drew which page.

The design pass works on one page at a time, so it needs that page's code and
nothing else. Two sources, and the order matters.

Execution is exact: every `add_slide` call has the script line that made it on
the stack, so the runner records it and the spans follow by construction. There
is no annotation to go stale and no way to pair a render with the wrong code.

The `# SLIDE <n>` comments are the fallback, and they earn their place in one
situation the record cannot cover: after a build that failed, the record still
describes the script that last built, while the comments survived the edit that
broke it. So a traceback is attributed through the comments.
"""

from __future__ import annotations

import re

from raven.ppt.contracts import PageSource

_MARKER = re.compile(r"^\s*#.*\bslide\s*0*(\d+)\b", re.IGNORECASE)


def page_sources(lines: list[str], created: list[int]) -> tuple[PageSource, ...]:
    """Page spans from the line that created each slide, as execution saw it.

    Slide k's block runs from the statement that created it to the one that
    created slide k+1, walked back over the blank and comment lines above so a
    page's banner travels with it.
    """
    spans = _spans_from_lines(lines, created)
    return tuple(PageSource(page=n, first_line=s, last_line=e) for n, (s, e) in sorted(spans.items()))


def page_blocks(lines: list[str]) -> dict[int, tuple[int, int]]:
    """Line span of each page's code from its `# SLIDE <n>` banner."""
    starts: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _MARKER.match(line)
        if match:
            number = int(match.group(1))
            # A banner is often three comment lines; keep the first of a run.
            if not starts or starts[-1][1] != number:
                starts.append((index, number))
    if not starts:
        return {}
    # A block starts at the top of its banner -- but only as far back as the
    # previous block's end, or the spans overlap and replacing one page corrupts
    # its neighbour.
    heads: list[int] = []
    for position, (start, _) in enumerate(starts):
        floor = heads[position - 1] if position else 0
        head = start
        while head > floor and lines[head - 1].lstrip().startswith("#") and not _MARKER.match(lines[head - 1]):
            head -= 1
        heads.append(head)
    blocks: dict[int, tuple[int, int]] = {}
    for position, (_, number) in enumerate(starts):
        end = heads[position + 1] if position + 1 < len(starts) else _tail_start(lines, heads[position])
        blocks[number] = (heads[position], end)
    return blocks


def broken_page(script: str, stderr: str) -> int | None:
    """The page whose block a failed build died in, or None.

    Not the runner's own frames, and not only the deepest frame: a block that
    calls a shared helper dies inside the helper, and the frame that names the
    page is the call site -- the innermost frame falling inside some page's block.
    A line in the shared prelude maps to nothing, and the failure stays what it
    was.
    """
    hits = re.findall(r'(?<!_run_)build\.py", line (\d+)', stderr or "")
    blocks = page_blocks(script.splitlines(keepends=True))
    for hit in reversed(hits):
        index = int(hit) - 1
        for number, (start, end) in blocks.items():
            if start <= index < end:
                return number
    return None


def level_that_separates_pages(chains: list[list[int]]) -> list[int]:
    """One line per slide, picked from the frame chains the runner recorded.

    Which frame identifies a page depends on how the script is shaped: top-level
    code per page puts it outermost, a function per page puts it deeper. So take
    the shallowest level whose lines are distinct across every slide -- that is
    the level which actually separates the pages. None qualifying means the pages
    cannot be told apart in the program at all, which is a finding, not a guess.
    """
    if not chains or any(not chain for chain in chains):
        return []
    for level in range(min(len(chain) for chain in chains)):
        candidate = [chain[level] for chain in chains]
        if len(set(candidate)) == len(candidate):
            return candidate
    return []


def _spans_from_lines(lines: list[str], created: list[int]) -> dict[int, tuple[int, int]]:
    if not created or any(line <= 0 for line in created):
        return {}
    spans: dict[int, tuple[int, int]] = {}
    for index, line in enumerate(created):
        start = line - 1  # the record is 1-based
        floor = created[index - 1] if index else 0  # never cross the previous creation
        for predicate in (_blank, _comment, _blank):
            while start > floor and predicate(lines[start - 1]):
                start -= 1
        spans[index + 1] = (start, len(lines))
    ordered = sorted(spans)
    for position, number in enumerate(ordered[:-1]):
        spans[number] = (spans[number][0], spans[ordered[position + 1]][0])
    last = ordered[-1]
    spans[last] = (spans[last][0], _tail_start(lines, spans[last][0]))
    return spans


def _blank(line: str) -> bool:
    return not line.strip()


def _comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _tail_start(lines: list[str], after: int) -> int:
    """Where the last page's block ends: the save call, or the file's end."""
    for index in range(after, len(lines)):
        line = lines[index]
        if "prs.save(" in line or (".save(" in line and "PPT_OUTPUT" in line):
            # Keep any comment banner above the save with the save itself.
            head = index
            while head > after and lines[head - 1].lstrip().startswith("#"):
                head -= 1
            return head
    return len(lines)
