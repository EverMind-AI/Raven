"""The vocabulary of the material: every number and spec-like name it states.

This is one half of the fact gate and it has to stay the same half. The gate
asks "did the material print this token?", so the index and the check must
tokenise and normalise identically -- two copies of these rules that drift by
one suffix produce a gate that rejects a number the source plainly states.
The regexes and :func:`canonical_number` are therefore the shared surface: the
gate is expected to import them from here rather than restate them.

Deterministic, no model involvement. The rules themselves are documented on
``SourceIndex``; what lives here is how they are applied to a text.
"""

from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_right
from pathlib import Path

from raven.ppt.contracts.sources import SourceIndex

NUMBER_RE = re.compile(
    r"""
    (?P<currency>[$€£¥])?
    # The grouped-thousands branch only reads a comma as a separator when what
    # precedes the digits is not itself alphanumeric. "44 FPS(单卡 A100,480p)" is a
    # spec, not a number: read as thousands it becomes 100,480, which appears in no
    # source, and it refused three pages of a live deck for saying 480p after A100.
    # `1,000km` and `$1,234,567` still group, because nothing alphanumeric precedes them.
    (?P<value>(?<![A-Za-z0-9])\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)
    \s*
    (?P<suffix>%|percent|billion|million|thousand|trillion|bn|[BMKk](?![a-zA-Z])|×|x(?![a-zA-Z]))?
    """,
    re.VERBOSE,
)

# Spec-ish names: alphanumeric model names (M4, A800, RTX4090, GPT-4o) and
# standalone acronyms (TOPS, RMSE). Ordinary capitalised words are deliberately
# not indexed -- natural language would drown the signal.
# A single *lowercase* letter before digits is a variable, not an identifier: `t0`,
# `t-1`, `x1`, `n2`. Five of those -- the axis labels on a timeline a deck drew itself
# -- refused a real deck as "model names introduced from outside knowledge". A single
# letter that is capitalised still gates, because that is `T5`, `A800`, `M4`, `R-50`.
ENTITY_RE = re.compile(
    r"\b("
    r"(?:[A-Za-z]{2,6}|[A-Z])[-_]?\d+(?:\.\d+)*[A-Za-z0-9]*"  # M4, A800, GPT-4o, ICLR2025
    r"|\d+[A-Za-z]{1,6}\b"  # 4o, 35B (also caught as number+suffix)
    r"|[A-Z]{2,6}\b"  # TOPS, RMSE, CPU
    r")"
)

# One or more all-caps words. One was the case that mattered: a deck labels a
# section AGENDA or TASK, the entity pattern reads it as an unknown acronym,
# and the phrase exemption below never sees it because a single word has no
# multi-word window to match. Twenty such labels blocked a real deck whose
# numbers were all correct -- and the author then left the tool and delivered
# the deck itself, past every gate.
CAPS_PHRASE_RE = re.compile(r"\b[A-Z]{2,}(?:[ \t]+[A-Z]{2,})*\b")

_SOURCE_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
_MAX_CAPS_PHRASE_WORDS = 6

_SUFFIX_SCALE = {
    "billion": "e9",
    "bn": "e9",
    "b": "e9",
    "million": "e6",
    "m": "e6",
    "thousand": "e3",
    "trillion": "e12",
    "k": "e3",
}

_IMPLICIT_SCALE_RE = re.compile(r"\bin\s+(?P<scale>thousand|million|billion|trillion)s?\b", re.I)


def normalise(text: str) -> str:
    """NFKC, which is the whole of it.

    The predecessor also replaced U+00A0 with a space on the way out. NFKC
    already maps every space-like codepoint it cares about -- NBSP, figure
    space, narrow NBSP, thin space -- to U+0020, so that line could not change
    a character.
    """
    return unicodedata.normalize("NFKC", text)


def canonical_number(value: str, suffix: str | None) -> str:
    """The index key for one numeric mention.

    ``30,972`` -> ``30972``; ``31 billion`` -> ``31e9``; ``14%`` -> ``14%``.
    A decimal's trailing zeros go, so ``31.0`` and ``31`` collide on purpose:
    the gate checks the identity of the printed figure, not float equality.
    """
    raw = value.replace(",", "")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    suffix_key = (suffix or "").lower()
    if suffix_key in ("%", "percent"):
        return f"{raw}%"
    if suffix_key in ("×", "x"):
        return f"{raw}x"
    scale = _SUFFIX_SCALE.get(suffix_key)
    return f"{raw}{scale}" if scale else raw


def iter_numbers(text: str):
    return NUMBER_RE.finditer(text)


def build_source_index(material_text: str) -> SourceIndex:
    """Index every number and spec-like name the materials mention.

    One pass over the text, not two. The predecessor built a second complete
    index over the same text with the heading lines stripped out, purely to
    read its ``numbers`` -- so every entity and every caps phrase (the
    expensive part: six window sizes over every line) was extracted twice and
    thrown away once. Which line a match sits on answers the same question.
    """
    text = normalise(material_text)
    line_starts, heading = _heading_lines(text)
    numbers: set[str] = set()
    stated: set[str] = set()
    for match in iter_numbers(text):
        keys = {canonical_number(match.group("value"), match.group("suffix"))}
        # The bare digits too, so "31 billion" in the source anchors a slide
        # that prints "31".
        keys.add(canonical_number(match.group("value"), None))
        if match.group("suffix") is None:
            scale = _implicit_scale(text, match.start())
            if scale is not None:
                keys.add(canonical_number(match.group("value"), scale))
        numbers |= keys
        if not heading[bisect_right(line_starts, match.start()) - 1]:
            stated |= keys
    return SourceIndex(
        numbers=frozenset(numbers),
        stated_numbers=frozenset(stated),
        entities=frozenset(match.group(0).upper() for match in ENTITY_RE.finditer(text)),
        caps_phrases=frozenset(_caps_phrases(text)),
        stated_chars=_stated_chars(text, line_starts, heading),
    )


def _stated_chars(text: str, line_starts: list[int], heading: list[bool]) -> int:
    """How much text the material itself carries, ingest's own anchors excluded."""
    total = 0
    for index, start in enumerate(line_starts):
        if heading[index]:
            continue
        end = line_starts[index + 1] if index + 1 < len(line_starts) else len(text)
        total += len(text[start:end].strip())
    return total


def _heading_lines(text: str) -> tuple[list[int], list[bool]]:
    """Line offsets, and whether each line is a markdown heading.

    The ingest writes its own page anchors into the material text ("## page
    3"), so a number that only appears in one of those was minted here rather
    than stated by the document. It still belongs in ``numbers`` -- a slide
    citing "p.7" is checked against that set -- but it must not authorise a
    data value.
    """
    starts = [0]
    starts.extend(match.end() for match in re.finditer(r"\n", text))
    flags = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        flags.append(text[start:end].lstrip().startswith("#"))
    return starts, flags


def _caps_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for line in text.splitlines():
        words = [match.group(0).casefold() for match in _SOURCE_WORD_RE.finditer(line)]
        # From one word up: a single ordinary word set in caps on a slide is
        # ordinary language if the materials use it at all, whatever its case.
        for size in range(1, min(_MAX_CAPS_PHRASE_WORDS, len(words)) + 1):
            phrases.update(" ".join(words[start : start + size]) for start in range(len(words) - size + 1))
    return phrases


def _line_before(text: str, line_start: int) -> tuple[int, str] | None:
    if line_start <= 0:
        return None
    end = line_start - 1 if text[line_start - 1] == "\n" else line_start
    start = text.rfind("\n", 0, end) + 1
    return start, text[start:end]


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3


def _scale_in_context(context: str) -> str | None:
    scales = {match.group("scale").casefold() for match in _IMPLICIT_SCALE_RE.finditer(context)}
    return next(iter(scales)) if len(scales) == 1 else None


def _implicit_scale(text: str, start: int) -> str | None:
    """The scale a bare number inherits from its table or its data rows.

    A financial table says "in millions" once, in a heading or a stub line,
    and then prints 30,972. Without this the slide has to invent a unit to
    quote it, which is exactly the rounding hallucination the gate exists to
    catch, so the scale is read from the surrounding block: upwards through a
    markdown table to the line above it, or upwards through consecutive lines
    that also carry numbers.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    if line_end < 0:
        line_end = len(text)
    current_line = text[line_start:line_end]
    context_start = line_start

    if _is_markdown_table_line(current_line):
        cursor = line_start
        while previous := _line_before(text, cursor):
            previous_start, previous_line = previous
            if not _is_markdown_table_line(previous_line):
                break
            context_start = previous_start
            cursor = previous_start

        previous = _line_before(text, context_start)
        if previous is not None and not previous[1].strip():
            previous = _line_before(text, previous[0])
        if previous is not None and not _is_markdown_table_line(previous[1]):
            if _IMPLICIT_SCALE_RE.search(previous[1]):
                context_start = previous[0]
    else:
        cursor = line_start
        while previous := _line_before(text, cursor):
            previous_start, previous_line = previous
            stripped = previous_line.strip()
            if not stripped or _is_markdown_table_line(previous_line):
                break
            if _IMPLICIT_SCALE_RE.search(previous_line):
                context_start = previous_start
                break
            if stripped.startswith("#") or not any(iter_numbers(previous_line)):
                break
            context_start = previous_start
            cursor = previous_start

    return _scale_in_context(text[context_start:start])


SCHEMA = "raven.ppt.source-index.v1"


def write_source_index(index: SourceIndex, path: Path, *, sources: list[str]) -> None:
    """Persist the index next to the materials it came from."""
    path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "sources": sources,
                "numbers": sorted(index.numbers),
                "stated_numbers": sorted(index.stated_numbers),
                "stated_chars": index.stated_chars,
                "entities": sorted(index.entities),
                "caps_phrases": sorted(index.caps_phrases),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def load_source_index(path: Path) -> SourceIndex:
    """Rehydrate an index written by :func:`write_source_index`."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SourceIndex(
        numbers=frozenset(raw.get("numbers", ())),
        stated_numbers=frozenset(raw.get("stated_numbers", ())),
        stated_chars=raw.get("stated_chars"),
        entities=frozenset(raw.get("entities", ())),
        caps_phrases=frozenset(raw.get("caps_phrases", ())),
    )
