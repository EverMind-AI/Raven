"""Choose which tool results a reviewer is shown, when a tail is the wrong choice.

A blind tail is right while a turn is short and wrong once it is long: a draft the
reviewer wrongly passed averaged 73 tool calls, so an 8-slot tail showed it about
11% of the evidence, and the results that had located the entity were the early ones
the tail dropped first.

``choose_by_coverage`` keeps the most recent ``keep_tail`` results -- the draft's own
reasoning hangs off those, and a pack without them stops more wrong drafts while
losing the right ones -- then fills the remaining slots greedily by how many of the
task's own terms a result is the first to cover.

Measured offline over 535 drafts, scored by a reviewer model reading each pack: at 16
slots x 4000 chars this stops 56.2% of wrong drafts against a tail's 48.6% (paired
+27/-11, p 0.014) with false stops falling 9.2% -> 8.3%. At 8 x 2000 the same
selection is not significant (p 0.34), and widening the tail without changing the
selection is worth nothing (p 0.61) -- the budget and the selection only pay together.
"""

from __future__ import annotations

import re

__all__ = ["choose_by_coverage", "task_terms"]

# Words, 3-4 digit numbers (years, counts), and CJK runs. CJK is cut into 2-gram
# shingles below because the run itself rarely reappears verbatim in a page.
_TERM = re.compile(r"[A-Za-z][A-Za-z'’\-]{2,}|\d{3,4}|[一-鿿]{2,}")

# Function words plus the vocabulary a task uses to ask rather than to specify.
# Keeping "answer"/"question"/"find" would make every result that discusses the task
# itself look informative, which is the opposite of what the selection is for.
_STOP = frozenset(
    (
        "the and for that with this from which who whom whose what when where how why not but you your his her its"
        " their they them was were been being are has have had did does do done can could would should may might"
        " must will about after also any because before between both each more most other some such than then there"
        " these those through under very while into over only same another one two three answer question name please"
        " give provide identify find first last known called based following"
    ).split()
)


def task_terms(task: str) -> list[str]:
    """Content terms of the turn's task, in order, deduplicated."""
    out: list[str] = []
    for token in _TERM.findall(task or ""):
        if re.fullmatch(r"[一-鿿]{2,}", token):
            out.extend(token[i : i + 2] for i in range(len(token) - 1))
        elif token.lower() not in _STOP:
            out.append(token.lower())
    return list(dict.fromkeys(out))


def choose_by_coverage(bodies: list[str], n: int, keep_tail: int, terms: list[str]) -> list[int]:
    """Indices of the ``n`` results to show, in the order they occurred.

    Returns every index when there are no more than ``n`` to begin with, so a short
    turn is unaffected by the setting. ``keep_tail`` is clamped to ``n``: a tail as
    long as the budget degrades to the recency pack rather than selecting nothing.
    """
    if n <= 0:
        return []
    if len(bodies) <= n:
        return list(range(len(bodies)))
    tail = max(0, min(keep_tail, n))
    chosen = set(range(len(bodies) - tail, len(bodies)))
    lowered = [b.lower() for b in bodies]
    hits = [{t for t in terms if t in body} for body in lowered]
    covered: set[str] = set()
    for i in chosen:
        covered |= hits[i]
    while len(chosen) < n:
        best, gain = None, 0
        for i in range(len(bodies)):
            if i in chosen:
                continue
            g = len(hits[i] - covered)
            if g > gain:
                best, gain = i, g
        if best is None:
            # Nothing left covers a term the pack does not already have, which is the
            # common case once the task is short: fall back to recency for the rest so
            # the budget is spent rather than left idle.
            for i in range(len(bodies) - 1, -1, -1):
                if i not in chosen:
                    chosen.add(i)
                    break
            else:  # pragma: no cover - unreachable while len(chosen) < n <= len(bodies)
                break
            continue
        chosen.add(best)
        covered |= hits[best]
    return sorted(chosen)
