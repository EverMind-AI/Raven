"""Read a final answer's marked span: does it abstain, and which answer does it name.

Both questions are asked of the span alone (``<answer>`` tag, ``\\boxed{}``, an
``Answer:`` line), never of the reasoning around it: a report that says "I could not
verify X" above a named answer has answered. Without a marker only a short reply is
read, since a long unmarked one is a report whose answer cannot be located.
"""

from __future__ import annotations

import re
import unicodedata

from research_flow.gates.final_shape import shape_final_answer

_UNMARKED_MAX_CHARS = 300

# Said at the span's opening, or anywhere in a short span: a long span that only
# mentions a doubt after naming its answer is an answer with a caveat.
_ABSTAIN = re.compile(
    r"\b(?:cannot|can\s?not|can't|could\s+not|couldn't|unable\s+to|not\s+able\s+to|impossible\s+to|failed\s+to)\b"
    r"[^.;\n]{0,60}?\b(?:determin|identif|verif|confirm|find|locat|establish|pin\s+down|answer|resolv|name)"
    r"|\bno\s+(?:verifiable|verified|definitive|confirmed|conclusive|reliable|identifiable|supported)\b"
    r"|\b(?:insufficient|not\s+enough|inconclusive)\s+(?:evidence|information|data)\b"
    r"|^(?:unknown|undetermined|indeterminate|indeterminable|unverifiable|unverified|unresolved|not\s+found)\b"
    # CJK forms spelled as escapes: cannot determine / confirm / find / identify / verify,
    # failed to determine / find, not enough evidence / information.
    "|\u65e0\u6cd5(?:\u786e\u5b9a|\u786e\u8ba4|\u627e\u5230|\u8bc6\u522b|\u6838\u5b9e|\u5224\u65ad)"
    "|\u672a\u80fd(?:\u786e\u5b9a|\u627e\u5230|\u8bc6\u522b|\u6838\u5b9e)"
    "|(?:\u8bc1\u636e|\u4fe1\u606f)\u4e0d\u8db3",
    re.I,
)
# A span that names a candidate while hedging it has answered.
_GUESS = re.compile(
    r"\bbest(?:\s+available)?\s+guess\b|\bmost\s+(?:likely|probable)\b|\bbest[-\s]supported\b"
    r"|\bcandidates?\b|\bclosest\b|\bbest[-\s]matching\b|\bprobably\b|\blikely\b|\bpoints?\s+(?:squarely\s+)?to\b"
    # most likely, presumably, closest, candidate
    "|\u6700\u53ef\u80fd|\u63a8\u6d4b|\u6700\u63a5\u8fd1|\u5019\u9009",
    re.I,
)
_OPENING_CHARS = 24
_SHORT_WORDS = 14

_DROP = re.compile(r"\([^)]*\)|\[[^\]]*\]|[*_`\"'\u2018\u2019\u201c\u201d]")
_TOKEN = re.compile(r"\w+")
_ARTICLES = frozenset({"the", "a", "an"})


def answer_span(text: str | None) -> str | None:
    """The answer the reply commits to, or ``None`` when it cannot be located."""
    shaped = shape_final_answer(text)
    span = shaped.span or shaped.visible.strip()
    if not span or (not shaped.span and len(span) > _UNMARKED_MAX_CHARS):
        return None
    # A tool call written out as text names nothing.
    return None if "<tool_call>" in span or "<function=" in span else span


def abstains(text: str | None) -> bool:
    """Whether the reply's answer says that no answer can be given."""
    span = answer_span(text)
    if not span:
        return False
    plain = _DROP.sub("", span).strip(" \t\n-:\u2014")
    if _GUESS.search(plain):
        return False
    hit = _ABSTAIN.search(plain)
    if hit is None:
        return False
    return hit.start() < _OPENING_CHARS or len(plain.split()) <= _SHORT_WORDS


def answer_key(text: str | None) -> frozenset[str] | None:
    """The words of the answer the reply names, for telling two answers apart."""
    span = answer_span(text)
    if not span or abstains(text):
        return None
    folded = unicodedata.normalize("NFKC", _DROP.sub(" ", span)).casefold()
    words = frozenset(_TOKEN.findall(folded)) - _ARTICLES
    return words or None


def same_answer(a: frozenset[str], b: frozenset[str]) -> bool:
    """One answer's words inside the other's: "Irma P. Hall" and "Irma Hall" agree."""
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return small <= large and (len(small) >= 2 or len(large) <= 2)
