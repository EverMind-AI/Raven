"""Is a finished turn a dead end -- i.e. worth running once more from scratch?

## Why the predicate is here and not in the product that uses it

The conditional rerun this predicate drives is the highest measured
pp-per-compute lever the research product has: +3.20pp at 1.25x compute,
measured four ways, where the next best flow change buys single digits at 2x.
Until now it existed only as an offline post-pass over persisted rows: something
scored a batch, picked the dead ones, and ran them again. A product user could
never reach it, however the config was written, because the mechanism was not in
the agent at all.

The predicate sits in the loop package rather than in the research plugin
because the loop is what will re-run the turn. A plugin decides *whether* its
turns may be re-run and supplies the labels for its own injected asks; the
classification itself is one implementation, here, so the switch and the test
cannot drift apart.

## Why the retry cannot lose points

A dead turn has no answer to damage. "Empty answer implies wrong" is a scoring
rule, not an observation, so the count of correct answers among dead turns is
structurally zero and a second attempt can only move it up. That is the same
argument recorded for the empty-response recovery (measured +3.33pp corpus /
+1.99pp web) and it is why this is the one mechanism that could be switched on
by class default without a fresh batch behind it.

The argument covers *re-running*, not *salvaging*. Squeezing an answer out of
the first attempt's leftovers was measured and it **loses** points: 23 of 25
real duds produced content, gold hit rate 0, all confidently wrong. It converts
a detectable zero into an undetectable one and, worse, empties this predicate's
own trigger. Retry the turn; never dress up its corpse.

## Ordering inside ``dead_reasons``

``stranded`` comes first because it is the only structural member. The others
are proxies: a status code, a length, a phrase. Each can be silently emptied by
a well-meaning fix - which is exactly what a finalization fallback would do to
the length test. ``stranded`` asks "did this trajectory end on the model's own
prose", which no amount of downstream answer-manufacturing can fake.
"""

from __future__ import annotations

from typing import Callable

from raven.agent.loop._shared import _HOOK_INJECTED_KEY
from raven.i18n.zh_lexicon import REFUSAL_OPENERS

NO_RESPONSE_FALLBACK = "I've completed processing but have no response to give."
"""What a turn says when the model produced no answer but a tool had already delivered one.

Lives here rather than beside its emitter because it is a predicate constant with two
readers a thousand lines apart: the loop writes it, and the dead-end test below reads
it. Rewording it in the emitter alone silently empties the trigger, and the symptom is
a batch that looks healthier.
"""

REFUSAL_MARKERS: tuple[str, ...] = REFUSAL_OPENERS
"""Answer strings that mean the model declined rather than researched.

The strings themselves live in ``raven.i18n.zh_lexicon`` because they are Chinese, and
that module is where this repo keeps the Chinese forms its engines recognise. Matching
is substring, not prefix - the disclaimer is sometimes led by a courtesy sentence.
"""

AskKind = Callable[[object], "str | None"]
"""Names the harness question an injected turn carries, or ``None``.

Injected by the caller rather than imported, because only the product that writes the
asks knows how they are worded. Absent, a harness-injected tail is still recognised
structurally and labelled ``harness_ask_unknown``: the boolean never depends on
wording, only the sub-label does.
"""


def _text_of(content: object) -> str:
    """Flatten a message body to text, blocks included.

    Content is a plain string on most backends and a list of typed blocks on the
    channel-separated ones, where the prose sits in ``{"type": "text", "text": ...}``
    entries.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    return "" if content is None else str(content)


def stranded_of(messages: list[dict], *, ask_kind: AskKind | None = None) -> tuple[bool, str | None]:
    """Did this trajectory fail to end on the model's own prose?

    Returns ``(stranded, stranded_on)``; ``stranded_on`` is ``None`` exactly when
    ``stranded`` is False, so the sub-label never doubles as "ended in a way we could
    not name".

    The test is structural, not textual. The first version only recognised a
    harness-injected question as the tail and missed two other shapes on a replay of one
    360-item arm: 5 and 4 items that ended on a ``tool`` result (the model called a tool,
    got the result, and never spoke again once the recovery budget ran out), plus one
    trajectory holding nothing but the question. Widened to "did not end on model prose",
    it stands in a strict-containment relation to the text-matched no-response test:
    77/77 and 78/80, exact equality on one arm. Containment is the right relation - one
    predicate reads a phrase out of the answer afterwards, this one reads the shape - so
    equality would be the surprising outcome, not the reassuring one.

    This reads correctly only because the loop does not persist contentless assistant
    turns. If that ever changes, this changes with it.
    """
    msgs = [m for m in messages if isinstance(m, dict) and m.get("role")]
    last = msgs[-1] if msgs else {}
    spoke_last = (
        last.get("role") == "assistant"
        and bool(_text_of(last.get("content")).strip())
        and not last.get(_HOOK_INJECTED_KEY)
    )
    stranded = bool(msgs) and not spoke_last
    if not stranded:
        return False, None
    if last.get(_HOOK_INJECTED_KEY):
        named = ask_kind(last.get("content")) if ask_kind is not None else None
        return True, named or "harness_ask_unknown"
    if last.get("role") == "tool":
        return True, "tool_result"
    if last.get("role") == "assistant":
        return True, "tool_call" if last.get("tool_calls") else "empty_assistant"
    return True, "question"


def dead_reasons(
    *,
    messages: list[dict],
    final_content: str | None,
    status: str = "completed",
    ask_kind: AskKind | None = None,
) -> list[str]:
    """Which dead-end tests this finished turn hit. Empty list = a live answer.

    ``status`` is the turn outcome's status, and only ``"error"`` counts: the turn blew
    up rather than finished.

    ``"interrupted"`` is deliberately NOT dead, and the reason is a name collision worth
    spelling out. An offline reader triggers on "the run did not end cleanly", but that
    is the *process* outcome (killed, crashed); the turn outcome's ``"interrupted"``
    means the turn hit its iteration cap or wall clock. That routes through the
    exhaustion path, which makes a wrap-up call and returns a real answer. Reading the
    two as one field made a budget-exhausted turn re-run the whole turn on a FRESH
    budget, silently doubling the wall clock that had just fired; the max-iteration
    tests caught it as 6 iterations where 3 were configured.

    An exhausted turn is still dead when it is *also* empty or stranded - the wrap-up
    call can fail - and then the tests below say so on their own. That is the right way
    round: the budget is not the evidence, the missing answer is.
    """
    out: list[str] = []
    stranded, stranded_on = stranded_of(messages, ask_kind=ask_kind)
    if stranded:
        out.append(f"stranded:{stranded_on or 'unknown'}")
    text = (final_content or "").strip()
    if not text:
        out.append("empty_answer")
    elif NO_RESPONSE_FALLBACK in text:
        # The emptiness test alone is not enough once an appendix is on: the loop fills
        # a turn whose model said nothing after a tool answered with this sentence, and
        # the research appendix then appends a full trail to it. The result is several
        # hundred characters of real content wrapped around "no answer" - which passes
        # every length test while being exactly the case this lever exists for.
        out.append("no_response")
    if status == "error":
        out.append("status:error")
    if any(m in text for m in REFUSAL_MARKERS):
        out.append("refusal_string")
    return out


__all__ = [
    "NO_RESPONSE_FALLBACK",
    "REFUSAL_MARKERS",
    "AskKind",
    "dead_reasons",
    "stranded_of",
]
