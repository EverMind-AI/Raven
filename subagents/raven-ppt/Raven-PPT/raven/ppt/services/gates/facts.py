"""The fact gate: every claim on a page traced back to the materials, verbatim.

Two hallucination shapes dominate the measurements this was built against.
World-knowledge fill-ins: a spec name, a model number or a date that reads
plausibly and appears nowhere in the sources. And derived numbers: a figure the
materials state as $30,972 million reprinted as $31 billion, which is arithmetic
the deck was never asked to do and a reader cannot check. Both are refusals
rather than warnings, because both are a statement about the source material
that the source material does not make -- the one class of defect a nicer layout
cannot mitigate.

Deterministic on purpose: the model never gets to argue with it. What it gets
back is the token, the sentence around it, and what to do instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from raven.ppt.contracts.findings import Audience, Finding, Severity
from raven.ppt.contracts.sources import SourceIndex

# The tokenisers come from the ingest rather than being written again here.
# Both sides of this gate have to normalise identically -- the question is
# whether the material printed this token -- and the predecessor kept two
# copies of the rules, on either side of a file format whose field names had
# already drifted apart. A drift there does not look like a bug; it looks like
# a deck of accurate numbers failing the gate.
from raven.ppt.services.ingest.facts import (
    CAPS_PHRASE_RE,
    ENTITY_RE,
    canonical_number,
    iter_numbers,
    normalise,
)

# Bare small integers are allowed without a source: list ordinals, slide
# numbering, "3 steps" style counts. Anything above, or anything carrying a
# unit / percent / currency, must trace to the materials.
_FREE_INT_MAX = 20


# Spec-ish entities: alphanumeric model names (M4, A800, RTX4090, GPT-4o) and
# standalone acronyms (TOPS, RMSE). Ordinary capitalized words are NOT gated --
# natural language would drown the signal.

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

_CURRENCY_DIMENSIONS = {"$": "usd", "€": "eur", "£": "gbp", "¥": "jpy"}


@dataclass(frozen=True)
class NumberMention:
    """One numeric mention, with enough of its shape to compare two of them."""

    text: str
    value: float
    canonical: str
    currency: str | None
    dimension: str


def number_mentions(text: str) -> list[NumberMention]:
    """Every number in a string, with its unit and scale kept apart.

    Two figures that print the same and mean different things -- 32 percent and
    32 million -- have to stay distinguishable, so the dimension travels with
    the value rather than being recovered from the surrounding prose later.
    """
    mentions: list[NumberMention] = []
    for match in iter_numbers(normalise(text)):
        raw_value = match.group("value")
        suffix = match.group("suffix")
        currency = match.group("currency")
        suffix_key = (suffix or "").casefold()
        if suffix_key in {"%", "percent"}:
            dimension = "percent"
        elif suffix_key in {"x", "×"}:
            dimension = "multiple"
        else:
            scale = _SUFFIX_SCALE.get(suffix_key, "")
            dimension = ":".join(part for part in (_CURRENCY_DIMENSIONS.get(currency or "", ""), scale) if part)
            dimension = dimension or "count"
        mentions.append(
            NumberMention(
                text=match.group(0).strip(),
                value=float(raw_value.replace(",", "")),
                canonical=canonical_number(raw_value, suffix),
                currency=currency,
                dimension=dimension,
            )
        )
    return mentions


def check_text(
    slide_text: str,
    index: SourceIndex,
    *,
    page: int | None = None,
    extra_allowed: set[str] | None = None,
    allow_free_int: bool = True,
) -> list[Finding]:
    """Every number and entity in `slide_text` that the materials never state.

    `extra_allowed` covers deck-local tokens that are legitimately not in the
    materials: the requested slide count, theme names, agenda numbering.
    """
    text = normalise(slide_text)
    allowed = {token.upper() for token in (extra_allowed or set())}
    findings: list[Finding] = []
    grounded_caps_spans = _grounded_caps_spans(text, index)

    covered: list[tuple[int, int]] = []
    for match in iter_numbers(text):
        value, suffix = match.group("value"), match.group("suffix")
        canonical = canonical_number(value, suffix)
        covered.append(match.span())
        plain = canonical.rstrip("%x")
        is_free_int = (
            suffix is None and match.group("currency") is None and plain.isdigit() and int(plain) <= _FREE_INT_MAX
        )
        if is_free_int and allow_free_int:
            continue
        if canonical in index.numbers or canonical.upper() in allowed:
            continue
        token = match.group(0).strip()
        findings.append(
            _finding(
                page,
                "number",
                token,
                _context_window(text, *match.span()),
                f"number {token!r} does not appear in the materials; copy the source figure verbatim "
                "(same unit, same rounding) or drop the claim",
            )
        )

    for match in ENTITY_RE.finditer(text):
        span = match.span()
        # Skip pure-number entity hits already handled above.
        if any(span[0] >= start and span[1] <= end for start, end in covered):
            continue
        token = match.group(0)
        if token.upper() in index.entities or token.upper() in allowed:
            continue
        if token.isalpha() and any(span[0] >= start and span[1] <= end for start, end in grounded_caps_spans):
            continue
        if token.isalpha():
            # A bare acronym is usually vocabulary, not a claim. The cut used to be
            # length -- CPU and AI passed at three letters, SOTA was refused at four
            # -- and a live outline lost a round to exactly that: "state-of-the-art
            # on 5/7 benchmarks" is not a fact anyone can check a source for. What
            # carries a checkable claim is a number or an alphanumeric identifier
            # (A800, GPT-4o, T5, R-50), and those still refuse. This still reports,
            # because an invented dataset or organisation is worth seeing.
            findings.append(
                Finding(
                    kind="unfamiliar_name",
                    severity=Severity.WARNING,
                    page=page,
                    audience=Audience.AUTHOR,
                    message=(
                        f"{token!r} does not appear in the materials. Ordinary vocabulary is fine; if it names "
                        "a dataset, a model or an organisation, this deck should not be the first to say so"
                    ),
                    detail={"claim": "name", "token": token, "context": _context_window(text, *span)},
                )
            )
            continue
        findings.append(
            _finding(
                page,
                "entity",
                token,
                _context_window(text, *span),
                f"{token!r} is not mentioned in the materials; do not introduce model names, spec "
                "identifiers, or version numbers from outside knowledge",
            )
        )
    return findings


def fact_findings(
    deck_text: list[tuple[int, str]],
    index: SourceIndex | None,
    *,
    extra_allowed: set[str] | None = None,
) -> list[Finding]:
    """Fact-gate a finished deck, one piece of copy at a time.

    Takes the deck's text rather than its path because the gate is about the
    words, and reading them off the built file is `measure.deck_text`'s job. A
    deck with no index behind it -- nothing ingested, or ingest never run -- is
    not gated at all: refusing every number because there is nothing to check it
    against would leave the author with no move.

    An index that was built and came back empty is the same situation, and it has
    a real cause: sources that are image-only. A scan yields no text, so the only
    way to read it is to look at the page -- which the author can do, the pages
    being in the figure catalogue -- and every figure read that way would then be
    refused as invented. Ingest already reports image-only sources as a
    `text_layer` warning, so the author is not told the material was read.

    "Empty" is read off `stated_chars`, not off the token sets: a document of pure
    prose with no digits in it also has no numbers to check against, and there a
    figure on a slide is invented and must still be refused. And it is exactly zero
    rather than a floor -- a floor is a cliff a real but very short source falls off,
    which measured as a one-line note disarming the gate.
    """
    if index is None or index.stated_chars == 0:
        return []
    return [
        finding
        for page, text in deck_text
        for finding in check_text(text, index, page=page, extra_allowed=extra_allowed)
    ]


def _finding(page: int | None, claim: str, token: str, context: str, message: str) -> Finding:
    return Finding(
        kind="fact",
        severity=Severity.BLOCKING,
        page=page,
        audience=Audience.AUTHOR,
        message=message,
        detail={"claim": claim, "token": token, "context": context},
    )


def _context_window(text: str, start: int, end: int, radius: int = 40) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)].strip()


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
    """A scale a table heading states once and its rows inherit.

    "Financial highlights (in millions)" over a column of bare numbers is how
    every results table is written, and without this the slide's "$25 million"
    would not anchor against the source's "25". Bounded on purpose: the scan
    stops at a blank line, a heading, or a line carrying no numbers, because a
    scale that leaks across a paragraph boundary would authorize any figure
    anywhere below it.
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


def _source_caps_phrases(text: str) -> set[str]:
    phrases: set[str] = set()
    for line in text.splitlines():
        words = [match.group(0).casefold() for match in _SOURCE_WORD_RE.finditer(line)]
        for size in range(2, min(_MAX_CAPS_PHRASE_WORDS, len(words)) + 1):
            phrases.update(" ".join(words[start : start + size]) for start in range(len(words) - size + 1))
    return phrases


def _grounded_caps_spans(text: str, index: SourceIndex) -> list[tuple[int, int]]:
    """Spans of all-caps text whose words the materials use in that order.

    A deck sets a label in caps -- TOTAL REVENUES -- and the entity pattern sees
    two unknown acronyms. The exemption is the materials' own vocabulary, not
    all-caps amnesty: the phrase has to appear, in order, in the sources.
    """
    spans = []
    for match in CAPS_PHRASE_RE.finditer(text):
        if " ".join(match.group(0).casefold().split()) in index.caps_phrases:
            spans.append(match.span())
    return spans
