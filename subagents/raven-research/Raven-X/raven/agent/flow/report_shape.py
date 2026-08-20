"""Report-template shape: a per-turn reminder, and a deterministic bar on it.

``final_shape.report_structure`` asks the model for a three-section report
(``## Answer`` / ``## Findings`` / ``## Limitations``) and dr@3.6 added the rule
that the template outranks a format the question itself asks for. Both live in
the system prompt, and a system prompt is the weakest place to put an
instruction that a conversation can argue with:

* **Recency loses to it.** Measured on this repo's own demo sessions (30 turns
  carrying the template, dr@3.5+): 8/14 research turns well-formed, but only
  2/9 of the turns the conversation gate ruled non-research. Those are the
  reformat/expand follow-ups, and the sessions where they failed had two or
  three earlier replies that were outlines - the model's own history is a
  stronger few-shot than one clause fifteen thousand tokens up the context.
* **Nothing checks it.** The verify gate reviews claims against evidence, not
  layout, and it is wrapped in ``GatedHook`` so it does not run on the
  non-research turns at all. The template was asked for and then never read
  back by anything.

So this module adds the two cheapest things that fix those two facts, and
nothing more.

**The reminder** (``render_reminder``) rides the current user message, the same
seam and the same lifetime as the research memo: injected at assembly, stripped
before persist, so it never accumulates across turns and never becomes history.
It is ~60 tokens of the same instruction, sitting where recency helps instead of
hurting.

**The bar** (:class:`ReportShapeGate`) is a deterministic markdown check - no
model call, no evidence, no latency - so unlike the verify gate it can afford to
run on *every* turn, which is exactly where the template was losing. A draft
missing a section bounces back once with an instruction to rewrite it into the
template while keeping every finding, source and caveat.

What it deliberately does **not** do, and why:

* **It never rewrites the draft itself.** Restructuring a finished answer is the
  failure class this repo has measured twice - an upstream framework's
  extraction stage carried gold on 71/120 questions into 58/120 boxed fields,
  dropping 10.83pp while inventing nothing, and dr@1.6's salvage seam failed
  identically. The rewrite is asked of the model, in the generation where it can
  still consult its own evidence; this module only decides whether to ask.
* **It bounces only for a MISSING section.** A decorated heading
  (``## 1. Answer``, ``## **Answer**``, ``## Findings (as a slide outline)`` - the
  last is a real shape from these sessions), a translated heading (``## 回答``),
  a heading at the wrong level, or three sections in the wrong order are all
  recorded and all shipped. Each bounce
  costs a full generation, and a reader loses nothing to a numbered or bolded
  heading; only an absent section is missing content the template promised.
  The ruler is deliberately lax in one direction and not the other: judging a
  present section absent spends a generation on punctuation, judging an absent
  one present ships one malformed reply. The second is the cheaper mistake, so
  the matcher takes it.
* **A bounce leaves no history.** The rejected draft and the rewrite request are
  injected as ``_recovery_synthetic`` scaffolding: the re-sample reads them, the
  session never stores them. Persisting them would contradict the paragraph
  above - the outline the bar just rejected would sit in the next turn's context
  as an example of what to write. Its length is kept (``draft_chars``): against
  the delivered ``final_shape.visible_chars`` that is the rewrite's shrink, and
  it is the only way to ask whether a bounce cost evidence, since the draft
  itself is gone from every record.
* **It never bounces an empty draft.** An answerless terminal belongs to
  ``ForcedFinalizeGate``, which runs ahead of this one. "May improve a record,
  never blank one" is the same rule ``final_shape`` states, one layer up.
* **It bounces at most once per turn**, and fails open on anything unexpected.
"""

from __future__ import annotations

import logging
import re

from raven.agent.flow.answer_text import closing_tag_bar, visible_answer
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

#: Section headings the template promises, in the order it promises them.
REPORT_SECTIONS: tuple[str, ...] = ("Answer", "Findings", "Limitations")

# Delimiters so ``AgentLoop._save_turn`` can take the reminder back out. Same
# reasoning as the research memo's: the block is rebuilt at every assembly, so
# persisting it would accumulate - turn three would carry two stale copies as
# history plus a fresh one. Suffix-anchored on the way out (see strip_reminder),
# which is what lets it compose with the memo and recovery blocks: those are
# prepended and stripped by prefix, this one is appended and stripped by suffix.
REMINDER_OPEN = "[report format reminder]"
REMINDER_CLOSE = "[/report format reminder]"

_REMINDER_BODY = (
    "Reply with the three-section report: `## Answer`, `## Findings`, "
    "`## Limitations`, in that order, all three present. If this message asks "
    "for another shape - an outline, slides, a table, JSON, one word - that "
    "shape goes inside the sections, not in place of them."
)

# Any heading level, any decoration around the name. Every one of these
# deviations shows up in real replies and none costs the reader anything, so they
# are measured here and tolerated by the gate rather than paid for with a
# re-generation. ``##`` is what the clause asks for; a model that writes ``###``
# still delivered the section.
_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*$", re.M)

# Leading decoration: a list number (``1.`` / ``2)``), bold markers, an emoji, a
# bullet. Stripped before matching. Numbered and bolded headings are among the
# most common things a model does to a template it was given, and treating them
# as absent sections would make the bar's measured price mostly the price of
# punctuation - which is the number that decides whether it ships on.
_LABEL_NOISE_RE = re.compile(r"^(?:[\W_]*\d+[.)])?[\W_]*")

# Matched anywhere in the cleaned label, not just at its start: ``## Key
# Findings`` and ``## The Answer`` are the section. Singular stems so
# ``## Limitation`` counts, and the left guard is ``[a-z]`` rather than ``\b`` so
# a CJK-prefixed heading (``## 答案 Answer``) still matches - CJK is a word
# character, and ``\b`` would refuse the boundary.
#
# The Chinese alternates are tolerance, not endorsement. Product traffic here is
# largely Chinese - the session that motivated this whole module was - and a model
# answering in Chinese translates its headings. Reading those as three absent
# sections would bounce an entire language for the spelling of its headings, which
# is the same mistake as bouncing ``## 1. Answer``, one order of magnitude up.
# ``annotated`` still records the deviation, so a batch can ask how often it happens.
_SECTION_RE = {
    name: re.compile(rf"(?<![a-z]){stem}|{alt}", re.I)
    for name, stem, alt in (
        ("Answer", "answer", "回答|答案|结论"),
        ("Findings", "finding", "发现|研究发现|调研"),
        ("Limitations", "limitation", "局限|限制|不足"),
    )
}


def render_reminder() -> str:
    """The per-turn reminder block, delimiters included."""
    return f"{REMINDER_OPEN} {_REMINDER_BODY} {REMINDER_CLOSE}"


def strip_reminder(content: str) -> str:
    """Remove an injected reminder from a message before it is persisted.

    Suffix-anchored and delimiter-exact, for the reason ``strip_memo`` gives for
    being prefix-anchored and delimiter-exact: a looser match that cut at a blank
    line would leave half a block behind on some inputs, and half a block is
    worse than either whole outcome. A message that does not end in the closing
    delimiter is returned untouched.
    """
    if not content:
        return content
    stripped = content.rstrip()
    if not stripped.endswith(REMINDER_CLOSE):
        return content
    start = stripped.rfind(REMINDER_OPEN)
    if start < 0:
        return content
    return stripped[:start].rstrip()


class ReportShape:
    """What one draft's markdown says about the template. Pure; no I/O.

    ``missing`` is the only field the gate acts on. The rest exist so a batch can
    ask how often the model deviated in ways nobody bounced - a deviation that is
    tolerated but never counted is indistinguishable from one that never happened,
    which is the shape that made ``dedup_skipped`` unreadable for two versions.
    """

    __slots__ = ("present", "missing", "annotated", "off_level", "in_order", "empty")

    def __init__(self, text: str) -> None:
        found: dict[str, tuple[int, bool, bool]] = {}
        for order, (hashes, body) in enumerate(_HEADING_RE.findall(text or "")):
            label = body.strip()
            cleaned = _LABEL_NOISE_RE.sub("", label)
            # No ``break``: one heading can satisfy two sections
            # (``## Findings and Limitations``, ``## 结论与局限``) and is counted
            # for both, which is the same direction as the tolerance above - the
            # reader has both sections, and refusing them would spend a
            # generation on a conjunction. The pair then shares one ``order``, so
            # a combined heading can read ``in_order=False`` on a layout nobody
            # would call out of order; that is a recorded deviation, not a bounce.
            for name in REPORT_SECTIONS:
                if name not in found and _SECTION_RE[name].search(cleaned):
                    # ``annotated`` compares the RAW label: the decoration the
                    # matcher just ignored is still a deviation, and a tolerated
                    # deviation nobody counts reads exactly like one that never
                    # happened.
                    found[name] = (order, label.lower() != name.lower(), len(hashes) != 2)
        self.empty = not (text or "").strip()
        self.present = tuple(n for n in REPORT_SECTIONS if n in found)
        self.missing = tuple(n for n in REPORT_SECTIONS if n not in found)
        self.annotated = tuple(n for n in self.present if found[n][1])
        self.off_level = tuple(n for n in self.present if found[n][2])
        seen = [found[n][0] for n in self.present]
        self.in_order = seen == sorted(seen)

    @property
    def well_formed(self) -> bool:
        return not self.missing

    def counters(self) -> dict[str, object]:
        """Observer payload. Read-only; recorded beside the answer."""
        return {
            "well_formed": self.well_formed,
            "present": list(self.present),
            "missing": list(self.missing),
            "annotated_headings": list(self.annotated),
            "off_level_headings": list(self.off_level),
            "in_order": self.in_order,
        }


_REWRITE_PROMPT = (
    "Your reply above is missing the required section(s): {missing}. Rewrite it "
    "as the three-section report - `## Answer`, `## Findings`, `## Limitations`, "
    "in that order, all three present - keeping every finding, every source URL "
    "and every caveat you already wrote. Do not shorten it, do not drop evidence, "
    "and do not research anything new. If this turn was asked for another shape "
    "(an outline, slides, a table), keep that content inside `## Findings`."
)


class ReportShapeGate(AgentHook):
    """Bounce a terminal draft that is missing a template section, once.

    Deliberately **not** wrapped in ``GatedHook``: it is the one DR observer that
    must run on a non-research turn, because that is the stratum where the
    template loses (2/9 well-formed against 8/14 on research turns). It is safe
    there in a way the other observers are not - it makes no model call, reads no
    evidence, and its whole verdict is a markdown parse of text the turn already
    produced.

    Ordered after ``DraftReviewerGate`` so substance is settled before layout: on
    a turn the reviewer rejects, its bounce short-circuits the chain (first
    rollback wins) and this gate reads the revision instead of the draft that was
    already going to be replaced.
    """

    def __init__(self, *, closing_tag_required: bool = False) -> None:
        self._closing_tag_required = closing_tag_required

    @property
    def name(self) -> str:
        return "ReportShapeGate"

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if getattr(ctx.response, "has_tool_calls", False):
            return HookDecision()
        draft = visible_answer(
            getattr(ctx.response, "content", None) or "",
            closing_tag_required=closing_tag_bar(
                self._closing_tag_required,
                getattr(ctx.response, "reasoning_content", None),
            ),
        )
        if not draft:
            # An answerless terminal is ForcedFinalizeGate's, and a gate that
            # bounced it would be asking a turn with nothing to say to say it in
            # three sections.
            return HookDecision()

        shape = ReportShape(draft)
        # ``report_shape_gate``, not ``report_shape``: the loop seam records the
        # shape that SHIPPED under the latter, and these are two different facts.
        # Same split the verify gate draws - the delivered verdict is near
        # constant, what varies is the friction it took to get there.
        state = ctx.metadata.setdefault("report_shape_gate", {"bounces": 0})
        # A joined string, not a list: ``_scalar_snapshot`` exports scalars only
        # and drops everything else without a word, which is how three
        # ForcedFinalizeGate counters went missing and a downstream scorer never
        # fired. This field is the one that says WHY a bounce happened, and the
        # observer writing it cannot see that it was dropped.
        # Union across the turn's passes, not the latest read. Overwriting blanks
        # the field exactly when the bar WORKED: a bounce whose rewrite landed
        # exported ``bounces: 1, missing_seen: ""`` and lost the one fact that
        # says what the generation was spent on. The cost is that on a
        # ``shipped_malformed`` turn the union cannot say which half triggered
        # the bounce and which survived the rewrite; the delivered
        # ``report_shape.missing`` gives the residual, and the trigger set is
        # exact whenever the rewrite worked - which is the population the knob is
        # priced on.
        seen = {n for n in state.get("missing_seen", "").split(",") if n}
        seen.update(shape.missing)
        state["missing_seen"] = ",".join(n for n in REPORT_SECTIONS if n in seen)
        if shape.well_formed:
            return HookDecision()
        if state["bounces"] >= 1:
            # Fail-open, and say so in the record: the second malformed draft
            # ships. "Asked twice and still shipped" and "never asked" are
            # different states, and only the first one is evidence that the
            # prompt-side fix has run out of road.
            state["shipped_malformed"] = True
            return HookDecision(notes=["report_shape: still malformed after one rewrite; shipping"])

        state["bounces"] += 1
        # The other half of the shrink ratio: ``final_shape`` stamps the
        # delivered ``visible_chars``, and without the pre-rewrite size nothing
        # can ask whether the rewrite dropped evidence - the one failure this
        # module is built to avoid, asserted in _REWRITE_PROMPT and unverifiable
        # until now. The draft itself leaves no trace to fall back on: it is
        # synthetic scaffolding, so it never reaches the session, and benchmarks
        # run with tracing off, so the journal checkpoint is not there either.
        state["draft_chars"] = len(draft)
        logger.warning(
            "report-shape: draft missing %s; bouncing back for one rewrite",
            ", ".join(shape.missing),
        )
        return HookDecision(
            rollback=True,
            rollback_inject=[
                # ``_recovery_synthetic``: visible to the re-sample, gone before
                # the turn is persisted. Without it the malformed draft becomes
                # permanent history - and this module's whole argument is that an
                # earlier outline in the history outweighs a clause in the system
                # prompt, so a bounce would plant the very few-shot it just paid a
                # generation to remove. It also stops a bounced turn from
                # persisting its answer twice.
                {"role": "assistant", "content": draft, "_recovery_synthetic": True},
                {
                    "role": "user",
                    "content": _REWRITE_PROMPT.format(missing=", ".join(shape.missing)),
                    "_recovery_synthetic": True,
                },
            ],
            notes=[f"report_shape: missing {', '.join(shape.missing)}, rewriting"],
        )


__all__ = [
    "REMINDER_CLOSE",
    "REMINDER_OPEN",
    "REPORT_SECTIONS",
    "ReportShape",
    "ReportShapeGate",
    "render_reminder",
    "strip_reminder",
]
