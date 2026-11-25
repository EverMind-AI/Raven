"""The report's markdown, read into blocks and inline spans for its reading page.

A research report is written under a contract: an answer blockquote, ``##`` sections,
paragraphs, lists, tables, links, emphasis and code. This module reads that subset and
nothing wider, so the page needs no markdown library. Text it cannot classify stays a
paragraph: the page then shows the characters the reply holds, never fewer.

Raw HTML is not a construct here. A tag in the report is text, and the page escapes it.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_RULE_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE_RE = re.compile(r"^ {0,3}>[ \t]?")
_ITEM_RE = re.compile(r"^( {0,3})([-*+]|\d{1,9}[.)])([ \t]+|$)")
_DELIMITER_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


# A heading that names a list of sources: the fixed English and Chinese words, and the
# others a model writes for the same list. The English word closes the heading, so a
# body heading that only holds it ("Open-source options", "Sources of error") is not one.
_SOURCES_HEADING_RE = re.compile(
    r"(?:^|[\W_])(?:sources?|references?|citations?|bibliography)[\W_]*$"
    "|\u6765\u6e90|\u5f15\u7528|\u53c2\u8003\u6587\u732e|\u53c2\u8003\u8d44\u6599",
    re.I,
)


def names_sources(heading: str) -> bool:
    """Whether a section's heading names a list of sources (``Sources``, ``References``)."""
    return _SOURCES_HEADING_RE.search(heading) is not None


@dataclass(frozen=True)
class Block:
    """One block of the report.

    ``kind`` is ``heading``, ``paragraph``, ``code``, ``quote``, ``list``, ``table`` or
    ``rule``. ``text`` is the block's inline markdown (a heading's label, a paragraph) or
    a code block's body. ``children`` holds a quote's blocks, ``items`` a list's items
    and ``rows`` a table's rows, the header first.
    """

    kind: str
    text: str = ""
    level: int = 0
    ordered: bool = False
    start: int = 1
    language: str = ""
    children: tuple[Block, ...] = ()
    items: tuple[tuple[Block, ...], ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    aligns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Span:
    """One inline span: ``text``, ``code``, ``strong``, ``em``, ``del``, ``link`` or ``break``."""

    kind: str
    text: str = ""
    url: str = ""
    children: tuple[Span, ...] = field(default=())


def parse_blocks(text: str) -> tuple[Block, ...]:
    """The blocks of ``text``, in order."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    return tuple(_blocks(lines))


def split_chunks(lines: list[str]) -> list[list[str]]:
    """``lines`` as the groups blank lines part, a fenced code block kept whole in its group.

    Each group can be kept or left out without changing how the others read: a code
    block cut at a blank line of its own would leave a fence open over what follows.
    """
    chunks: list[list[str]] = []
    chunk: list[str] = []
    closes: re.Pattern[str] | None = None
    for line in lines:
        if closes is not None:
            chunk.append(line)
            if closes.match(line):
                closes = None
        elif not line.strip():
            if chunk:
                chunks.append(chunk)
            chunk = []
        else:
            chunk.append(line)
            if fence := _FENCE_RE.match(line):
                closes = _fence_close(fence.group(1))
    if chunk:
        chunks.append(chunk)
    return chunks


def section_starts(lines: list[str]) -> list[int]:
    """The index of each line that opens a ``#`` or ``##`` section, the lines of a fenced code block left out."""
    starts: list[int] = []
    closes: re.Pattern[str] | None = None
    for index, line in enumerate(lines):
        if closes is not None:
            if closes.match(line):
                closes = None
        elif fence := _FENCE_RE.match(line):
            closes = _fence_close(fence.group(1))
        elif (heading := _HEADING_RE.match(line)) and len(heading.group(1)) <= 2:
            starts.append(index)
    return starts


def _fence_close(mark: str) -> re.Pattern[str]:
    return re.compile(r"^ {0,3}" + re.escape(mark[0]) + "{" + str(len(mark)) + r",}[ \t]*$")


def _at(lines: list[str], index: int) -> str | None:
    return lines[index] if index < len(lines) else None


def _table_opens(line: str, following: str | None) -> bool:
    if "|" not in line or following is None or "|" not in following or not _DELIMITER_RE.match(following):
        return False
    return len(_cells(following)) == len(_cells(line))


def _interrupts(line: str, following: str | None) -> bool:
    """Whether ``line`` ends the paragraph above it. A numbered item does only when it starts at 1."""
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _RULE_RE.match(line) or _QUOTE_RE.match(line):
        return True
    if item := _ITEM_RE.match(line):
        return not item.group(2)[0].isdigit() or item.group(2)[:-1] == "1"
    return _table_opens(line, following)


def _blocks(lines: list[str]) -> list[Block]:
    out: list[Block] = []
    at = 0
    while at < len(lines):
        line = lines[at]
        if not line.strip():
            at += 1
        elif fence := _FENCE_RE.match(line):
            at = _code(lines, at, fence, out)
        elif heading := _HEADING_RE.match(line):
            out.append(Block("heading", text=heading.group(2).strip(), level=len(heading.group(1))))
            at += 1
        elif _RULE_RE.match(line):
            out.append(Block("rule"))
            at += 1
        elif _QUOTE_RE.match(line):
            at = _quote(lines, at, out)
        elif _table_opens(line, _at(lines, at + 1)):
            at = _table(lines, at, out)
        elif _ITEM_RE.match(line):
            at = _list(lines, at, out)
        else:
            at = _paragraph(lines, at, out)
    return out


def _code(lines: list[str], at: int, fence: re.Match[str], out: list[Block]) -> int:
    closes = _fence_close(fence.group(1))
    body: list[str] = []
    at += 1
    while at < len(lines) and not closes.match(lines[at]):
        body.append(lines[at])
        at += 1
    out.append(Block("code", text="\n".join(body), language=fence.group(2)))
    return at + 1


def _quote(lines: list[str], at: int, out: list[Block]) -> int:
    inner: list[str] = []
    while at < len(lines) and _QUOTE_RE.match(lines[at]):
        inner.append(_QUOTE_RE.sub("", lines[at], count=1))
        at += 1
    out.append(Block("quote", children=tuple(_blocks(inner))))
    return at


def _cells(line: str) -> tuple[str, ...]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return tuple(cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(body))


def _align(cell: str) -> str:
    left, right = cell.startswith(":"), cell.endswith(":")
    return "center" if left and right else "right" if right else "left" if left else ""


def _table(lines: list[str], at: int, out: list[Block]) -> int:
    header = _cells(lines[at])
    width = len(header)
    aligns = tuple(_align(cell) for cell in _cells(lines[at + 1]))
    rows = [header]
    at += 2
    while at < len(lines) and lines[at].strip() and "|" in lines[at]:
        rows.append((_cells(lines[at]) + ("",) * width)[:width])
        at += 1
    out.append(Block("table", rows=tuple(rows), aligns=aligns))
    return at


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _next_at(lines: list[str], at: int) -> int | None:
    return next((index for index in range(at, len(lines)) if lines[index].strip()), None)


def _next_text(lines: list[str], at: int) -> str | None:
    index = _next_at(lines, at)
    return lines[index] if index is not None else None


def _list(lines: list[str], at: int, out: list[Block]) -> int:
    first = lines[at].lstrip(" ")
    number = first[: len(first) - len(first.lstrip("0123456789"))]
    ordered = bool(number)
    items: list[tuple[Block, ...]] = []
    while at < len(lines):
        marker = _ITEM_RE.match(lines[at])
        if marker is None or marker.group(2)[0].isdigit() != ordered:
            break
        inside = len(marker.group(0)) if marker.group(3) else len(marker.group(0)) + 1
        body = [lines[at][inside:]]
        at += 1
        while at < len(lines):
            line = lines[at]
            if not line.strip():
                # The whole run of blank lines at once: a scan for each line of it is quadratic.
                ahead = _next_at(lines, at + 1)
                if ahead is None or _indent(lines[ahead]) < inside:
                    break
                body.extend([""] * (ahead - at))
                at = ahead
                continue
            elif _indent(line) >= inside:
                body.append(line[inside:])
            elif _ITEM_RE.match(line) or _interrupts(line, _at(lines, at + 1)) or not body[-1].strip():
                break
            else:
                body.append(line.strip())
            at += 1
        items.append(tuple(_blocks(body)))
        # A blank line between two items of the same list keeps the list open.
        ahead = _next_text(lines, at)
        sibling = _ITEM_RE.match(ahead) if ahead is not None else None
        if sibling is None or sibling.group(2)[0].isdigit() != ordered:
            break
        while at < len(lines) and not lines[at].strip():
            at += 1
    out.append(Block("list", ordered=ordered, start=int(number) if ordered else 1, items=tuple(items)))
    return at


def _paragraph(lines: list[str], at: int, out: list[Block]) -> int:
    body: list[str] = []
    while at < len(lines) and lines[at].strip():
        if body and _interrupts(lines[at], _at(lines, at + 1)):
            break
        raw = lines[at]
        hard = raw.endswith("  ") or raw.rstrip().endswith("\\")
        body.append(raw.strip().rstrip("\\").rstrip() + ("\\" if hard else ""))
        at += 1
    out.append(Block("paragraph", text="\n".join(body).rstrip("\\")))
    return at


# ---------------------------------------------------------------------------
# Inline spans
# ---------------------------------------------------------------------------

_ESCAPABLE = frozenset("\\`*_{}[]()<>#+-.!|~\"'&$%^=/:;?@,")
# A bare address stops at whitespace, a quote, a bracket and CJK punctuation.
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\u3000-\u303f\uff00-\uffef]+", re.I)
_ANGLE_URL_RE = re.compile(r"<((?:https?://|mailto:)[^\s<>]+)>", re.I)
_TRAILING_PUNCTUATION = ".,;:!?*_~"
_PAIRS: tuple[tuple[str, str], ...] = (("**", "strong"), ("__", "strong"), ("~~", "del"), ("*", "em"), ("_", "em"))
_BREAK_RE = re.compile(r"(?=\n\n)")


class _Scan:
    """What one text's inline scan has already learned, so no stretch of it is read once per mark.

    A report's text is a model's, and a model that degenerates repeats one mark for
    kilobytes. Read naively, every unclosed ``*`` or ``[`` scans the rest of the text.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self._closes: dict[int, int] | None = None
        self._paren_closes: dict[int, int] | None = None
        self._breaks: list[int] | None = None
        #: Per mark, the position up to which no later opening of it can close.
        self.unclosed: dict[str, int] = {}

    def bracket_close(self, at: int) -> int:
        """Where the ``[`` at ``at`` closes, or -1."""
        if self._closes is None:
            self._closes = {}
            open_at: list[int] = []
            scan = 0
            while scan < len(self.text):
                char = self.text[scan]
                if char == "\\":
                    scan += 2
                    continue
                if char == "[":
                    open_at.append(scan)
                elif char == "]" and open_at:
                    self._closes[open_at.pop()] = scan
                scan += 1
        return self._closes.get(at, -1)

    def paren_close(self, at: int) -> int:
        """Where the ``(`` at ``at`` closes on its own line, or -1."""
        if self._paren_closes is None:
            self._paren_closes = {}
            open_at: list[int] = []
            for scan, char in enumerate(self.text):
                if char == "\n":
                    open_at.clear()
                elif char == "(":
                    open_at.append(scan)
                elif char == ")" and open_at:
                    self._paren_closes[open_at.pop()] = scan
        return self._paren_closes.get(at, -1)

    def break_after(self, at: int) -> int:
        """The first blank line at or after ``at``, or the text's length."""
        if self._breaks is None:
            self._breaks = [found.start() for found in _BREAK_RE.finditer(self.text)]
        index = bisect_left(self._breaks, at)
        return self._breaks[index] if index < len(self._breaks) else len(self.text)


def parse_inline(text: str) -> tuple[Span, ...]:
    """The inline spans of ``text``."""
    spans: list[Span] = []
    plain: list[str] = []
    state = _Scan(text)

    def flush() -> None:
        if plain:
            spans.append(Span("text", text="".join(plain)))
            plain.clear()

    def emit(span: Span) -> None:
        flush()
        spans.append(span)

    at = 0
    while at < len(text):
        char = text[at]
        if char == "\\" and at + 1 < len(text) and text[at + 1] == "\n":
            emit(Span("break"))
            at += 2
        elif char == "\\" and at + 1 < len(text) and text[at + 1] in _ESCAPABLE:
            plain.append(text[at + 1])
            at += 2
        elif char == "`":
            ticks = _run(text, at, "`")
            close = _closing_ticks(text, at + ticks, ticks)
            if close == -1:
                plain.append(text[at : at + ticks])
                at += ticks
            else:
                emit(Span("code", text=text[at + ticks : close].strip().replace("\n", " ")))
                at = close + ticks
        elif char in "![" and (link := _link(state, at + (char == "!"))) is not None:
            label, url, end = link
            emit(Span("link", url=url, children=parse_inline(label) or (Span("text", text=url),)))
            at = end
        elif char == "<" and (angle := _ANGLE_URL_RE.match(text, at)):
            emit(Span("link", url=angle.group(1), children=(Span("text", text=angle.group(1)),)))
            at = angle.end()
        elif char in "hH" and (at == 0 or not text[at - 1].isalnum()) and (bare := _BARE_URL_RE.match(text, at)):
            url = _trim_url(bare.group(0))
            emit(Span("link", url=url, children=(Span("text", text=url),)))
            at += len(url)
        elif char in "*_~" and (pair := _emphasis(state, at)) is not None:
            kind, inner, end = pair
            emit(Span(kind, children=parse_inline(inner)))
            at = end
        else:
            plain.append(" " if char == "\n" else char)
            at += 1
    flush()
    return tuple(spans)


def _run(text: str, at: int, char: str) -> int:
    end = at
    while end < len(text) and text[end] == char:
        end += 1
    return end - at


def _closing_ticks(text: str, start: int, ticks: int) -> int:
    at = start
    while (found := text.find("`", at)) != -1:
        run = _run(text, found, "`")
        if run == ticks:
            return found
        at = found + run
    return -1


def _trim_url(url: str) -> str:
    while url and url[-1] in _TRAILING_PUNCTUATION:
        url = url[:-1]
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def _link(state: _Scan, at: int) -> tuple[str, str, int] | None:
    """``[label](address)`` at ``text[at]``: its label, its address and where it ends."""
    text = state.text
    if not text.startswith("[", at):
        return None
    close = state.bracket_close(at)
    if close == -1 or not text.startswith("(", close + 1):
        return None
    end = state.paren_close(close + 1)
    if end == -1:
        return None
    target = text[close + 2 : end].strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    elif target:
        target = target.split(None, 1)[0]
    if not target:
        return None
    return text[at + 1 : close], target, end + 1


def _emphasis(state: _Scan, at: int) -> tuple[str, str, int] | None:
    """The emphasis that opens at ``text[at]``: its kind, its inner text and where it ends."""
    text = state.text
    for mark, kind in _PAIRS:
        if not text.startswith(mark, at):
            continue
        char, width = mark[0], len(mark)
        start = at + width
        if start >= len(text) or text[start].isspace():
            continue
        if char == "_" and at > 0 and text[at - 1].isalnum():
            continue
        # Whether a later mark closes does not depend on where the emphasis opened, so
        # an opening that found none up to a point rules out every opening before it.
        if start < state.unclosed.get(mark, 0):
            continue
        end = state.break_after(start)
        scan = start
        while (found := text.find(char, scan, end)) != -1:
            run = _run(text, found, char)
            scan = found + run
            if found == start or text[found - 1].isspace() or text[found - 1] == "\\":
                continue
            if char == "_" and scan < len(text) and text[scan].isalnum():
                continue
            # A run of the other width belongs to an emphasis nested inside this one.
            if (width == 2 and run < 2) or (width == 1 and run == 2):
                continue
            return kind, text[start : scan - width], scan
        state.unclosed[mark] = end
    return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

# A step's own name where a sentence can start: "Step 2", and the two zh spellings of it.
_STEP_DIGITS = "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"
_STEP_RE = re.compile(
    r"(?:^|(?<=[\u3002\uff1b\uff01\uff1f.;!?\n]))[ \t]*(?P<mark>(?P<bold>\*\*|__)?(?P<name>"
    rf"\u7b2c[ \t]*(?P<nth>\d{{1,2}}|[{_STEP_DIGITS}])[ \t]*\u6b65"
    rf"|\u6b65\u9aa4[ \t]*(?P<step>\d{{1,2}}|[{_STEP_DIGITS}])"
    r"|Step[ \t]+(?P<en>\d{1,2})))",
    re.IGNORECASE,
)
_STEPS_FROM = 3


def _nth(mark: re.Match[str]) -> int:
    digit = mark.group("nth") or mark.group("step") or mark.group("en")
    return int(digit) if digit.isdigit() else _STEP_DIGITS.index(digit) + 1


def step_number(text: str) -> int | None:
    """The number of the step ``text`` opens with by name ("Step 2", and the two zh spellings of it), or None."""
    mark = _STEP_RE.match(text)
    return _nth(mark) if mark is not None else None


def _steps(text: str) -> tuple[str, list[str]] | None:
    """A paragraph that runs its steps together: what stands before them, and each step's markdown.

    Three or more steps count, each named where a sentence starts and numbered on from
    0 or 1 without a gap. A sentence that only mentions a step breaks the count, and
    the paragraph stays as it is.
    """
    marks = list(_STEP_RE.finditer(text))
    if len(marks) < _STEPS_FROM:
        return None
    numbers = [_nth(mark) for mark in marks]
    if numbers[0] not in (0, 1) or any(later != earlier + 1 for earlier, later in zip(numbers, numbers[1:])):
        return None
    starts = [mark.start("mark") for mark in marks]
    lead = text[: starts[0]].strip()
    steps = [text[start:end].strip() for start, end in zip(starts, [*starts[1:], len(text)])]
    # A cut inside a link, a code span or an emphasis would change what the text says.
    cut = "".join(span_text(parse_inline(part)) for part in (lead, *steps))
    if "".join(cut.split()) != "".join(span_text(parse_inline(text)).split()):
        return None
    return lead, steps


def _plain_parts(chunk: list[str]) -> list[tuple[list[str], str | None]] | None:
    """A chunk of headings and paragraphs as their lines, a paragraph with its text; None for any other chunk."""
    if not chunk[0][:1].strip():
        return None
    parts: list[list[str]] = []
    heads: list[bool] = []
    for line in chunk:
        head = _HEADING_RE.match(line) is not None
        if head or not parts or heads[-1]:
            parts.append([line])
            heads.append(head)
        else:
            parts[-1].append(line)
    blocks = parse_blocks("\n".join(chunk))
    if [block.kind for block in blocks] != ["heading" if head else "paragraph" for head in heads]:
        return None
    return [(part, None if head else block.text) for part, head, block in zip(parts, heads, blocks)]


def list_steps(text: str) -> tuple[str, int]:
    """``text`` with each paragraph that runs its steps together written as a list; and how many were.

    The report file and the reading page both hold this text, so the two read the
    same. Only a paragraph that starts at the margin, alone or under its heading, is
    rewritten: one inside a quote, a list item or a table is left as it is. Each step
    keeps its words, and its name is set strong unless the report did that.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    at = 0
    listed = 0
    for chunk in split_chunks(lines):
        while not lines[at].strip():
            out.append(lines[at])
            at += 1
        at += len(chunk)
        parts = _plain_parts(chunk) or [(chunk, None)]
        for index, (part, paragraph) in enumerate(parts):
            steps = _steps(paragraph) if paragraph is not None else None
            if steps is None:
                out.extend(part)
                continue
            listed += 1
            if steps[0]:
                out.extend([steps[0], ""])
            for step in steps[1]:
                mark = _STEP_RE.match(step)
                if mark is not None and not mark.group("bold"):
                    step = f"**{mark.group('name')}**{step[mark.end() :]}"
                out.append("- " + step.replace("\n", "\n  "))
            if index + 1 < len(parts):
                out.append("")
    out.extend(lines[at:])
    return "\n".join(out), listed


# ---------------------------------------------------------------------------
# Numbered lists under a label line
# ---------------------------------------------------------------------------


def _numbered(line: str | None) -> int | None:
    """The number a line opens with as a list item at the margin, when the item has text."""
    marker = _ITEM_RE.match(line) if line is not None else None
    if marker is None or not marker.group(2)[0].isdigit() or not line[marker.end() :].strip():
        return None
    return int(marker.group(2)[:-1])


def part_numbered_lists(text: str) -> tuple[str, int]:
    """``text`` with a blank line above each numbered list that a paragraph holds; and how many were set.

    A numbered line that does not start at 1 cannot end the paragraph above it. So a
    label line directly above "13." makes the label and the entries one paragraph, in
    this reader and in any other. The lines count as a list when the first number
    goes on from the last numbered line above it in the section, or when the next
    line carries the next number. No word of the text changes.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    at = 0
    parted = 0
    last: int | None = None
    for chunk in split_chunks(lines):
        while not lines[at].strip():
            out.append(lines[at])
            at += 1
        at += len(chunk)
        closes: re.Pattern[str] | None = None
        cut = False
        for index, line in enumerate(chunk):
            if closes is not None:
                if closes.match(line):
                    closes = None
            elif fence := _FENCE_RE.match(line):
                closes = _fence_close(fence.group(1))
            elif _HEADING_RE.match(line):
                last = None
            elif (number := _numbered(line)) is not None:
                goes_on = last is not None and number == last + 1
                if (
                    index
                    and not cut
                    and number != 1
                    and (goes_on or _numbered(_at(chunk, index + 1)) == number + 1)
                    and parse_blocks("\n".join(chunk[:index]))[-1].kind == "paragraph"
                ):
                    out.append("")
                    parted += 1
                    cut = True
                last = number
            out.append(line)
    out.extend(lines[at:])
    return "\n".join(out), parted


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------


def span_text(spans: tuple[Span, ...]) -> str:
    """The characters a reader sees in ``spans``, with no markup."""
    parts: list[str] = []
    for span in spans:
        if span.kind in ("text", "code"):
            parts.append(span.text)
        elif span.kind == "break":
            parts.append(" ")
        else:
            parts.append(span_text(span.children))
    return "".join(parts)


def plain_units(blocks: tuple[Block, ...]) -> list[str]:
    """The report's text as a reader sees it: one unit per paragraph, heading, list item and table row."""
    units: list[str] = []
    for block in blocks:
        if block.kind in ("heading", "paragraph"):
            units.append(span_text(parse_inline(block.text)))
        elif block.kind == "code":
            units.append(block.text)
        elif block.kind == "quote":
            units.extend(plain_units(block.children))
        elif block.kind == "list":
            for item in block.items:
                units.append(" ".join(plain_units(item)))
        elif block.kind == "table":
            units.extend(" ".join(span_text(parse_inline(cell)) for cell in row) for row in block.rows)
    return [unit for unit in units if unit.strip()]


__all__ = [
    "Block",
    "Span",
    "list_steps",
    "names_sources",
    "parse_blocks",
    "parse_inline",
    "part_numbered_lists",
    "plain_units",
    "section_starts",
    "span_text",
    "split_chunks",
    "step_number",
]
