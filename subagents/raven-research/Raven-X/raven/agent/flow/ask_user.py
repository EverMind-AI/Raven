"""dr@3.4-askuser: one clarify round at the turn boundary (product surface).

Under ``askUser.delivery="handoff"`` (default) the tool call is a **signal**; the
turn boundary is the **transport**. Nothing blocks on a human.
``AskUserGate.before_execute_tools`` reads the proposed call and returns
``short_circuit_result``, so the questions become this turn's reply and the
user's next message arrives as an ordinary inbound. That is why the DR variant of
the tool (``flow/ask_user_tool.py``) does not block by default while
``tools/ask_user.py`` blocks for the gateway and the TUI.

``delivery="tool"`` swaps the transport, not the discipline: the gate grants the
tool exactly one broker round trip, the questions reach the user as a structured
prompt, and the answers return as the tool RESULT - so the same turn researches
on them, nothing below (pending, brief, chain) is spent, and the question text
never becomes the reply. With no broker wired the gate falls back to the handoff.

Why the short circuit lands in ``before_execute_tools`` and not later: the loop
drops the whole tool-call response there rather than persisting it, so no
dangling ``tool_calls`` reach a strict provider. It also means ``after_iteration``
never runs on a clarify turn, which is what makes the verify gate and the report
bar structurally unreachable here instead of exempted by a flag.

**Two ContextVars, in opposite directions**, for the same reason the conversation
module has three: ``AgentHookContext`` carries ``session_key`` and not the
``Session``, so a gate can neither read nor write session state.

* ``stash_pending_clarify`` / ``take_pending_clarify`` - gate to loop. The loop
  writes it onto ``session.metadata`` next to the research memo. Read-once, so a
  later turn that asked nothing cannot re-persist a stale pending.
* ``set_chain_round`` / ``chain_round`` - loop to gate. The gate needs the chain's
  incoming count to decide ``chain_exhausted``, and the count lives on the
  session. Set on EVERY turn, including to 0: a turn that inherited the previous
  chain's count would be refused a legitimate first question, silently.

**The chain count passes through the consume.** A handoff opens a chain at 1; the
turn that answers carries 1 in, and a second handoff there writes 2. Zeroing on
consume would make round two indistinguishable from round one and ``maxRounds >
1`` unreachable. A turn that researches normally closes the chain by not writing
a new pending, and the count is discarded with it - so the session's next,
unrelated research question gets the full budget. Scoping the budget to the
session instead would refuse it.

**The rendered blocks are prompt text.** ``render_handoff`` is the one artifact
that enters history verbatim, which makes it the independent variable of the
stratum dr@3.4 measured at 2/9 well-formed against 8/14 - a template that varies
would make the first reading unattributable. ``render_brief`` is computed from
the pending state, never asked of the model, and transcribes the user's reply
**verbatim** rather than asserting "the answer is X": that is what caps the cost
of a misread ``is_reply_to`` at one paragraph of stale context instead of a
fabricated question-answer pair.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from raven.agent.flow.conversation import is_research_turn
from raven.agent.flow.report_shape import REPORT_SECTIONS, ReportShape
from raven.agent.hook.base import AgentHook, AgentHookContext, HookDecision

logger = logging.getLogger(__name__)

_TOOL = "ask_user"

# Tools whose presence in this turn means research has already started. Checked
# in addition to the iteration number so ``firstIterationOnly=false`` still
# matches what the contract clause tells the model ("after your first search it
# is gone") instead of turning that sentence into a lie.
_SEARCH_TOOLS = ("web_search", "web_fetch")

# Wraps the injected brief so ``AgentLoop._save_turn`` can take it back out. Same
# lifetime as the research memo and the report reminder: rebuilt per turn,
# stripped before persist. Persisting it would accumulate, and the accumulated
# copy would be a Q/A pair sitting in history as an example to imitate.
BRIEF_OPEN = "[research brief]"
BRIEF_CLOSE = "[/research brief]"

# The scaffolding sentences, per language. Everything else in the two blocks is
# the model's own text, which already follows the question's language.
#
# Why the SCAFFOLDING follows it too. The handoff is the only prose in this
# feature a user reads directly, and product traffic here is largely Chinese
# (``report_shape.py`` says so, and tolerates translated headings for the same
# reason). An English lead-in above Chinese questions is a visibly machine-made
# reply. The brief is model-facing and follows the same switch for a different
# reason: it is transcription, and a label in one language over a quotation in
# another invites the model to translate the quotation.
#
# Two languages only, matching what ``_language_directive`` already supports. The
# alternative - a real language identifier - is a dependency for one sentence.
#
# ⚠️ Each variant is prompt text with its own bytes. Adding a third language adds
# a handoff sha, not just a string.
_HANDOFF_LEAD = {
    "en": "I will start researching as soon as these are settled:",
    "zh": "确认下面几点之后我就开始查：",
}
_OUTLINE_LEAD = {
    "en": "How I intend to go about it (say if you would rather I did not):",
    "zh": "我打算这样查（不合适的话直接说）：",
}
_BRIEF_LABELS = {
    "en": {
        "original": "Original question: {}",
        "asked": "I asked ({}): {}",
        "replied": "The user replied, verbatim: {}",
        "outline": "The approach I proposed, which the user did not reject:",
    },
    "zh": {
        "original": "原问题：{}",
        "asked": "我问过（{}）：{}",
        "replied": "用户回复原文：{}",
        "outline": "我提出的做法，用户没有否决：",
    },
}

# Same range ``spin_breaker._CJK_RUN_RE`` uses, spelled the same way.
_CJK_RE = re.compile(r"[一-鿿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
_QUESTION_MARK_RE = re.compile(r"[?？]")


def own_text(text: str) -> str:
    """The user's own words, with this build's injected blocks taken back off.

    ``AgentHookContext.turn_question`` is captured at loop entry, which is AFTER
    assembly - so on a dr@3.4 product arm it carries the report reminder (~250
    chars of English) and, on a later turn, the research memo and a previous
    brief. Three things go wrong if that string is used as the question:

    * the language switch counts the reminder's English words and picks ``en``
      for a Chinese question - observed on the first live run;
    * the brief transcribes the reminder as part of "the original question", and
      the reminder is a block this build strips everywhere else precisely so it
      does not accumulate;
    * ``is_reply_to``'s length guard scales off ``len(original_question)``, so an
      inflated question makes every reply "short" and silently disables the veto.

    Stripped in the same order ``_save_turn`` uses, and only here: ``turn_question``
    itself is what the verify gate and the finalize gate judge a draft against, so
    trimming it at the source would change what a reviewer reads on a measured arm.
    """
    from raven.agent.flow.conversation import strip_memo
    from raven.agent.flow.report_shape import strip_reminder

    body = strip_reminder(text or "")
    return strip_memo(strip_brief(body)).strip()


def scaffold_language(text: str) -> str:
    """``"zh"`` or ``"en"`` for one piece of user text. Never raises.

    CJK ideograph COUNT against Latin WORD count, not a character ratio. The two
    mixed shapes that actually occur are indistinguishable by ratio - "What is
    小红书's revenue" sits at 0.167 and "OpenRouter 的 deepseek-v4-flash 价格是多少"
    at 0.171 - because a Latin product name inflates the denominator exactly like
    Latin prose does. Counting words instead separates them: an English sentence
    spends many short words on grammar, a Chinese sentence quoting a product name
    spends one or two.

    Ties and text with no CJK at all are English: the questions themselves already
    carry the language, so the scaffolding is the smaller half of the decision and
    the safer default is the one every other constant in this repo uses.
    """
    body = text or ""
    cjk = len(_CJK_RE.findall(body))
    if cjk == 0:
        return "en"
    return "zh" if cjk >= len(_LATIN_WORD_RE.findall(body)) else "en"


# ---------------------------------------------------------------------------
# Handoffs across the seams the hook context cannot reach
# ---------------------------------------------------------------------------

_PENDING: ContextVar[dict[str, Any] | None] = ContextVar("dr_pending_clarify_stash", default=None)
_CHAIN_ROUND: ContextVar[int] = ContextVar("dr_clarify_chain_round", default=0)
_TURN_BRIEF: ContextVar[str] = ContextVar("dr_clarify_turn_brief", default="")

# The turn's verdict on an inbound pending, handed from ``_decide_turn_mode`` to
# the turn-end stamp. A ContextVar and NOT an AgentLoop attribute, for the reason
# ``_RESEARCH_TURN`` states in its own comment: one loop instance serves every
# session of a gateway and each turn runs in its own asyncio task, so an attribute
# lets one conversation's follow-up stamp its verdict on another's trajectory.
# ``_turn_mode`` next to it is an attribute and carries exactly that defect; this
# is not the place to add a second instance of it.
_VERDICT: ContextVar[tuple[str, float] | None] = ContextVar("dr_clarify_verdict", default=None)

# Whether this is the first turn of the conversation, handed from the loop (which
# has the Session) to the gate (which does not). ``mode="first_turn"`` makes the
# contract MANDATE a clarify round on exactly this turn, so it is the turn whose
# ``asked`` value is a compliance rate rather than a preference. Default False:
# a turn nobody marked is not a first turn, so the strict reading never fires by
# accident.
_FIRST_TURN: ContextVar[bool] = ContextVar("dr_clarify_first_turn", default=False)


def stash_pending_clarify(pending: dict[str, Any]) -> None:
    _PENDING.set(pending)


def take_pending_clarify() -> dict[str, Any] | None:
    """Take and clear. Read-once for the same reason as ``take_turn_rows``."""
    pending = _PENDING.get()
    _PENDING.set(None)
    return pending


def set_chain_round(value: int) -> None:
    _CHAIN_ROUND.set(max(0, int(value or 0)))


def chain_round() -> int:
    return _CHAIN_ROUND.get()


def set_turn_brief(text: str) -> None:
    _TURN_BRIEF.set(text or "")


def turn_brief() -> str:
    return _TURN_BRIEF.get()


def set_first_turn(value: bool) -> None:
    _FIRST_TURN.set(bool(value))


def is_first_turn() -> bool:
    return _FIRST_TURN.get()


def set_clarify_verdict(verdict: str | None, overlap: float = 0.0) -> None:
    _VERDICT.set(None if verdict is None else (verdict, overlap))


def clarify_verdict() -> tuple[str, float] | None:
    return _VERDICT.get()


# ---------------------------------------------------------------------------
# Pending state
# ---------------------------------------------------------------------------


@dataclass
class PendingClarify:
    """One open clarify round, as it is persisted on ``session.metadata``."""

    original_question: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    outline: list[dict[str, Any]] = field(default_factory=list)
    chain_round: int = 1
    asked_at: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "original_question": self.original_question,
            "questions": self.questions,
            "outline": self.outline,
            "chain_round": self.chain_round,
            "asked_at": self.asked_at,
        }

    @classmethod
    def from_metadata(cls, raw: Any) -> "PendingClarify | None":
        if not isinstance(raw, dict):
            return None
        # Same cleaning as the writer's path, not a shallower copy of it - see
        # ``clean_questions``. The count is deliberately not re-capped here.
        questions = clean_questions(raw.get("questions"))
        if not questions:
            # A pending with no question cannot be answered, so it is not a
            # pending. Guards a hand-edited session file rather than our own
            # writer: the gate refuses to open a chain without one.
            return None
        try:
            # Read back from a session FILE, so a hand-edited or truncated value
            # must not raise: this runs on the turn-entry path and an exception
            # here fails the whole turn, for a field whose only job is a counter.
            chain = int(raw.get("chain_round") or 1)
        except (TypeError, ValueError):
            chain = 1
        outline = raw.get("outline")
        return cls(
            original_question=str(raw.get("original_question") or ""),
            questions=questions,
            # ``goal`` required, not just ``dict``: both renderers subscript it
            # bare, and this constructor reads a session FILE - the one place its
            # own docstring says must not raise. A hand-edited or truncated entry
            # was reaching ``render_brief`` and taking the whole turn down with a
            # KeyError, the same shape as the ``options``-as-string bug.
            outline=[o for o in outline if isinstance(o, dict) and str(o.get("goal") or "").strip()]
            if isinstance(outline, list) else [],
            chain_round=chain,
            asked_at=str(raw.get("asked_at") or ""),
        )

    @property
    def question_text(self) -> str:
        return " ".join(str(q.get("question") or "") for q in self.questions)

    @property
    def reference_text(self) -> str:
        """Everything the handoff put in front of the user, options included.

        Separate from ``question_text`` because the two answer different
        questions. ``question_text`` is what was ASKED - it renders, it picks the
        scaffold language, and it sets how long a reply is expected to be.
        This is the vocabulary the user was HANDED, and an option list is
        precisely an instruction to reply in its words. Measuring the reply
        against the stems alone scored two real replies at exactly 0.000 -
        "technical detail" and "deep learning" lived only in the options - where
        the full reference scores them 0.214 and 0.286.
        """
        parts: list[str] = []
        for q in self.questions:
            parts.append(str(q.get("question") or ""))
            parts.extend(str(o) for o in (q.get("options") or []))
        return " ".join(parts)


# Options per question. Not a config knob: it bounds a RENDERING, and the render
# is what a user reads. A generation that emits sixty of them is malformed however
# it got there, and truncating is the only outcome that stays readable.
_MAX_OPTIONS = 8


def clean_questions(
    raw: Any, *, max_questions: int | None = None
) -> list[dict[str, Any]]:
    """Normalise a ``questions`` value into ``[{question, options}]``. Never raises.

    Shared by the two readers of this shape, which is the point: the writer's path
    (``parse_ask_user_args``, reading a generation) and the reader's path
    (``PendingClarify.from_metadata``, reading a session FILE) were cleaning to
    different depths, and the shallower one was the file reader - the side whose
    input nobody controls. ``options``-as-a-string reached ``reference_text`` there
    and expanded per character, which moves a reply-overlap score; a blank
    ``question`` reached ``render_brief`` and wrote ``I asked (1): None`` into the
    next turn's prompt.

    ``max_questions=None`` means "clean the shapes, keep the count". The file
    reader passes it: its input was already capped by the writer under whatever
    ``askUser.maxQuestions`` was set then, and re-capping on read would silently
    drop questions the user was really asked on a profile that raised the knob.
    """
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        text = ""
        options: list[str] = []
        if isinstance(item, dict):
            text = str(item.get("question") or "").strip()
            # ``isinstance(raw, list)`` here too, and this is the one that shipped
            # broken: a str is iterable, so ``options`` arriving as a STRING is read
            # as one option PER CHARACTER. Observed on live traffic - a model emitted
            # its remaining JSON as this field's value and the handoff rendered
            # ~700 single-character bullets. The question text itself was fine, so
            # the options are dropped and the question is kept.
            raw_options = item.get("options")
            options = (
                [str(o).strip() for o in raw_options if str(o).strip()][:_MAX_OPTIONS]
                if isinstance(raw_options, list)
                else []
            )
        elif isinstance(item, str):
            # A model that passed a bare string asked a real question; refusing
            # it on shape would spend the round trip and deliver nothing.
            text = item.strip()
        if not text:
            continue
        out.append({"question": text, "options": options})
        if max_questions is not None and len(out) >= max(1, max_questions):
            break
    return out


def parse_ask_user_args(
    arguments: Any,
    *,
    max_questions: int = 3,
    max_outline_items: int = 5,
) -> PendingClarify:
    """Read one ``ask_user`` call's arguments. Never raises.

    Defensive about shape rather than trusting the schema: the arguments come
    from a generation, and a hook that raises is swallowed by ``CompositeHook``
    (``composite.py``) into a silent no-op - the failure would be a feature that
    stopped working with nothing in the log to say so.
    """
    data = arguments if isinstance(arguments, dict) else {}
    # ``isinstance(raw, list)``, not truthiness: a str is iterable, so a model that
    # passed ``"questions": "which year?"`` would otherwise be read as one question
    # per CHARACTER - and each character is a non-empty str, so the bare-string
    # branch below accepted every one of them.
    raw_questions = data.get("questions")
    raw_outline = data.get("outline")
    questions = clean_questions(raw_questions, max_questions=max_questions)

    outline: list[dict[str, Any]] = []
    for item in raw_outline if isinstance(raw_outline, list) else []:
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            continue
        outline.append({
            "goal": goal,
            "evidence": str(item.get("evidence") or "").strip(),
            "why": str(item.get("why") or "").strip(),
        })
        if len(outline) >= max(1, max_outline_items):
            break

    return PendingClarify(questions=questions, outline=outline)


# ---------------------------------------------------------------------------
# Rendering — fixed templates, because they enter the trajectory
# ---------------------------------------------------------------------------


def render_handoff(pending: PendingClarify) -> str:
    """The turn's reply: the questions, and nothing that reads as a refusal.

    Three constraints, each paid for elsewhere in this repo:

    * The lead-in says the research starts as soon as the questions are answered.
      Without it the reply reads as declining the task, which is the failure
      ``_DR_MEASURED_GUIDANCE`` states outright ("a reply that declines to name a
      candidate is scored wrong every single time").
    * The outline section is not called a plan. ``CONTEXT.md`` keeps "plan" for
      the search plan, and one word doing two jobs is how a reading becomes
      unattributable.
    * No ``## Answer`` / ``## Findings`` / ``## Limitations`` headings. This text
      becomes history, and a partial three-section report sitting there is a
      worse few-shot for the next turn than no report at all - the exact
      mechanism report_shape.py measured at 2/9.

    ⚠️ The outline renders ``goal`` and nothing else, while the schema still asks
    for ``evidence`` and ``why``. That is deliberate, and the two halves must not
    be "reconciled" in either direction. Asking what evidence settles a step is
    what keeps the outline naming decisions rather than queries - drop the fields
    and the goals go vague - but rendering all three put 100+ characters on one
    line, and this section exists to be SCANNED. Both stay in the persisted
    pending, so the reasoning is still in the session record.
    """
    # One fixed lead-in per language, not one per question count: the whole value
    # of a fixed template is that the bytes are the same on every item of a batch.
    lang = scaffold_language(pending.original_question or pending.question_text)
    lines = [_HANDOFF_LEAD[lang], ""]
    for i, q in enumerate(pending.questions, start=1):
        lines.append(f"{i}. {q.get('question')}")
        for option in q.get("options") or []:
            lines.append(f"   - {option}")
    if pending.outline:
        lines.append("")
        lines.append(_OUTLINE_LEAD[lang])
        # ``goal`` only. ``evidence`` and ``why`` are collected and NOT rendered:
        # crammed onto one line they produced 100+ character steps, and this section
        # exists to be scanned for "is that the right plan" - three clauses per step
        # is harder to scan than one, not more informative.
        for i, step in enumerate(pending.outline, start=1):
            lines.append(f"{i}. {step['goal']}")
    return "\n".join(lines)


def render_brief(pending: PendingClarify, user_reply: str) -> str:
    """The block prepended to the turn that answers. Transcription, not inference.

    "What the user replied" is quoted verbatim and never re-stated as an answer
    to a particular question. When ``is_reply_to`` was wrong, the block then
    contains a paragraph that visibly is not an answer - one stale paragraph -
    instead of a fabricated pairing the model would take as settled fact. This is
    also the discipline the research memo states for itself: computed from a
    record, never asked of the model.
    """
    # The delimiters stay ASCII in both languages: they are the anchors
    # ``strip_brief`` and ``_save_turn`` match on, and a localised anchor would
    # leave the block unstripped - which means persisted, which means accumulating.
    label = _BRIEF_LABELS[scaffold_language(pending.original_question or pending.question_text)]
    lines = [BRIEF_OPEN]
    if pending.original_question:
        lines.append(label["original"].format(pending.original_question))
    for i, q in enumerate(pending.questions, start=1):
        lines.append(label["asked"].format(i, q.get("question")))
    reply = (user_reply or "").strip()
    if reply:
        lines.append(label["replied"].format(reply))
    if pending.outline:
        lines.append(label["outline"])
        lines += [f"- {step['goal']}" for step in pending.outline]
    lines.append(BRIEF_CLOSE)
    return "\n".join(lines)


def strip_brief(content: str) -> str:
    """Remove an injected brief before the message is persisted.

    Delimiter-based ``find`` rather than a prefix check. The brief IS the
    outermost prefix today, so ``startswith`` would work - but which block is
    outermost is a fact about the ORDER of three call sites in ``_save_turn``,
    not about this function, and a prefix match fails silently the day that order
    changes. That failure mode is the one ``strip_memo`` guards against with the
    same shape: a block that is not stripped is persisted and accumulates.
    """
    start = content.find(BRIEF_OPEN)
    if start < 0:
        return content
    end = content.find(BRIEF_CLOSE, start)
    if end < 0:
        # Half a block is worse than either whole outcome - the same call
        # ``strip_memo`` makes about a URL list containing a blank line.
        return content
    head = content[:start]
    tail = content[end + len(BRIEF_CLOSE) :].lstrip("\n")
    if head.strip() and tail:
        # The block sat between two pieces of real text; leave one blank line
        # rather than the two that removing a whole paragraph would leave.
        return f"{head.rstrip()}\n\n{tail}"
    return (head + tail).lstrip("\n")


# ---------------------------------------------------------------------------
# "Is this message the answer?" — a pure function, biased towards yes
# ---------------------------------------------------------------------------


def _bigrams(text: str) -> set[str]:
    flat = "".join(text.split()).lower()
    return {flat[i : i + 2] for i in range(len(flat) - 1)}


def reply_overlap(pending: PendingClarify, text: str) -> float:
    """Share of the new message's bigrams that also appear in the handoff.

    Measured against ``reference_text``, not ``question_text``: see that property
    for the two live replies this scored at 0.000.

    Exposed separately so the loop can RECORD it. The threshold has to be chosen
    from data, and the background rate is script-dependent in a way one constant
    cannot cover - measured on hand-written pairs:

    ==========================  =====
    English answer                0.56
    English "reformat that"       0.35
    English unrelated request     0.18-0.20
    Chinese answer                0.27
    Chinese unrelated request     0.00
    ==========================  =====

    Latin script shares function-word bigrams (``th``, ``he``, ``e_``) with any
    other Latin text, so its floor sits near 0.18; CJK bigrams are content-bearing
    and unrelated text scores 0. At the configured 0.05 the veto therefore fires
    on CJK and effectively never on English - which is the designed bias direction
    (see ``is_reply_to``), but it is a property to read off the recorded rate
    rather than to assume. ``pending_verdict`` is explicitly re-computable offline;
    this is the column that makes that possible.
    """
    theirs = _bigrams(text or "")
    if not theirs:
        return 1.0
    return len(theirs & _bigrams(pending.reference_text)) / len(theirs)


def is_reply_to(pending: PendingClarify, text: str, *, threshold: float = 0.05) -> bool:
    """Whether ``text`` answers the pending questions, rather than starting over.

    Character bigrams, not words: the questions can be Chinese, where "word
    overlap" needs a tokenizer, and a coarse predicate whose error costs one
    paragraph does not justify a new dependency (AGENTS.md 4).

    One veto, and the asymmetry is deliberate. A new request read as an answer
    costs a stale paragraph in the brief - bounded by the verbatim transcription
    above. An answer read as a new request throws away everything the user just
    said, and they cannot tell that it happened. So only a message that is BOTH
    substantial and lexically unrelated is refused; "option 2" is always an
    answer.

    ⚠️ It does NOT decide only the brief, and an earlier version of this note
    claimed it did. ``_consume_pending_clarify`` also calls ``set_chain_round(0)``
    on the ``new_request`` branch, and ``chain_round`` is what the gate reads for
    ``chain_exhausted`` - so with ``maxRounds: 1`` this verdict decides whether
    ``ask_user`` is back in the schema on the turn that follows a handoff, brief
    or no brief. The recorded ``reply_overlap`` is therefore partly censored by its
    own threshold, and an offline re-fit has to stratify on whether a second
    handoff happened.

    Whether the turn runs research stays with ``ConversationGate``, which exists
    precisely for the "reformat what you just wrote" message this predicate would
    misread.
    """
    body = (text or "").strip()
    if not body:
        return True
    if not pending.reference_text:
        return True
    # Scaled by what was ASKED, not by the user's original research question. The
    # original question's length says nothing about how long an answer to a
    # clarify should be, and it is usually the shorter of the two: "what is deep
    # learning" put the bar at 10 characters, so an 18-character selection came out
    # "substantial" and lost the exemption this guard exists to give it.
    substantial = len(body) > 0.6 * len(pending.question_text)
    if not substantial:
        return True
    return reply_overlap(pending, body) >= threshold


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class AskUserGate(AgentHook):
    """Turns one ``ask_user`` call into the turn's reply, and withholds the tool
    once asking is no longer allowed.

    Deliberately NOT wrapped in ``GatedHook``, and appended after the wrap so the
    exception is visible at the seam (same treatment as ``ReportShapeGate``).
    Wrapping it would stop it running on a non-research turn - which is exactly
    the turn where the tool has to be taken OUT of the schema. A gated version
    would leave the tool offered on every follow-up it never sees.
    """

    def __init__(
        self,
        *,
        mode: str = "when_needed",
        max_rounds: int = 1,
        first_iteration_only: bool = True,
        max_questions: int = 3,
        max_outline_items: int = 5,
        outline: bool = True,
        delivery: str = "handoff",
        tool: Any | None = None,
    ) -> None:
        self._mode = mode
        self._max_rounds = max(1, max_rounds)
        self._first_iteration_only = first_iteration_only
        self._max_questions = max_questions
        self._max_outline_items = max_outline_items
        # Effective, not configured: under ``delivery="tool"`` no prompt
        # surface asks for an outline (see ``DRAskUserTool``), so seeding the
        # state with the configured True would record a request that was
        # never made. ``parse_ask_user_args`` still reads one if a model
        # volunteers it, and ``has_outline`` records that separately.
        self._outline = outline and delivery != "tool"
        # ``delivery="tool"`` needs the registered tool instance: readiness is a
        # runtime fact (broker late-bound by the transport, conversation_id set
        # per turn) that only the tool can answer, and the grant it hands out is
        # what stops a withheld call from spending a round trip on its own.
        self._delivery = delivery
        self._tool = tool

    @property
    def name(self) -> str:
        return "AskUserGate"

    def _state(self, ctx: AgentHookContext) -> dict[str, Any]:
        """The turn's namespace, seeded so a quiet turn still reports.

        Every field is written whether or not the gate fired: "asked nothing" and
        "was not installed" are different states, and the observers exporter has
        already cost this repo a batch by making one read as the other.
        """
        state = ctx.metadata.setdefault("ask_user", {})
        state.setdefault("asked", False)
        state.setdefault("outline", self._outline)
        state.setdefault("mode", self._mode)
        # Seeded with the CONFIGURED value; the fire path overwrites it with what
        # actually happened, so "tool asked for, handoff delivered" stays visible.
        state.setdefault("delivery", self._delivery)
        state.setdefault("withheld", 0)
        state.setdefault("chain_round", chain_round())
        # The two fields that make ``asked`` readable under ``mode="first_turn"``:
        # on the turn the contract MANDATES a round, ``asked`` is a compliance rate,
        # and on every other turn it is a preference. One column cannot be both
        # unless the turn says which it is. Nothing can force a model to emit a tool
        # call, so this is the only honest instrument for the mode.
        state.setdefault("first_turn", is_first_turn())
        state.setdefault("ask_required", self._mode == "first_turn" and is_first_turn())
        return state

    @staticmethod
    def _searched(ctx: AgentHookContext) -> bool:
        """Has this TURN already retrieved anything?

        The scan starts at ``ctx.turn_base``, never 0. A full-list scan finds the
        previous turns' searches and withholds the tool on every turn from the
        second on - and turn two onwards is the only place this feature can work
        at all. ``fetch_gate.py`` paid for this exact bug and left the note.
        """
        for message in (ctx.messages or [])[ctx.turn_base or 0 :]:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool" and message.get("name") in _SEARCH_TOOLS:
                return True
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or call.get("name")
                if name in _SEARCH_TOOLS:
                    return True
        return False

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        # A grant is one response's authority. Any grant still standing at an
        # iteration boundary belonged to a call that never executed, and
        # leaving it would let a later, ungranted call in the same turn spend
        # it. Duck-typed like ``round_trip_ready``: tests hand the gate stubs.
        revoke = getattr(self._tool, "revoke_round_trip", None)
        if revoke is not None:
            revoke()
        state = self._state(ctx)
        offered = any(
            (t.get("function") or {}).get("name") == _TOOL for t in (ctx.tools or [])
        )
        if not offered:
            # The switch is on and the tool is not in the schema - almost always
            # ``tools.disabledTools``. Reported rather than passed over: a rule
            # whose action is a no-op reads downstream exactly like a rule that
            # acted and did not help (``fetch_gate.py``).
            state["tool_absent"] = True
            return HookDecision()

        if state.get("asked"):
            # The round already happened - a broker round trip keeps the turn
            # going, so this hook runs again. Taking the tool back out is the
            # round's designed lifecycle, not a refusal, and it stays out of
            # ``withheld``: that column is the refusal rate, and a handoff
            # turn (which never reaches here after asking) reports 0.
            state["withdrawn_after_ask"] = True
            return HookDecision(
                modified_tools=[
                    t for t in (ctx.tools or [])
                    if (t.get("function") or {}).get("name") != _TOOL
                ]
            )

        reason = None
        if not is_research_turn():
            reason = "non_research_turn"
        elif self._first_iteration_only and (ctx.iteration or 1) > 1:
            reason = "not_first_iteration"
        elif self._searched(ctx):
            reason = "searched"
        elif chain_round() >= self._max_rounds:
            reason = "chain_exhausted"
        if reason is None:
            # Which iteration the tool was legitimately on offer. Checked again in
            # ``before_execute_tools``: withholding a tool from the schema does
            # not stop a model from naming it anyway, and honouring such a call
            # would hand the turn away on an iteration this gate had closed.
            state["allowed_at"] = ctx.iteration or 1
            return HookDecision()

        state["withheld"] = int(state.get("withheld") or 0) + 1
        # FIRST reason wins, not the last. Observed on the first live two-turn run:
        # iteration 1 was withheld as ``chain_exhausted`` - the one informative
        # reason on the turn - and iterations 2..10 then overwrote it with
        # ``not_first_iteration``, which says nothing, because after iteration 1
        # that is true of every turn. Read together with ``allowed_at``: absent
        # means this reason is why the tool was never on offer at all.
        state.setdefault("withheld_reason", reason)
        return HookDecision(
            modified_tools=[
                t for t in (ctx.tools or [])
                if (t.get("function") or {}).get("name") != _TOOL
            ]
        )

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        proposed = list(getattr(ctx.response, "tool_calls", None) or [])
        mine = [c for c in proposed if getattr(c, "name", "") == _TOOL]
        if not mine:
            return HookDecision()
        state = self._state(ctx)
        if state.get("allowed_at") != (ctx.iteration or 1):
            # Called on an iteration where the tool was withheld. Left to execute
            # so the model reads the fallback string and goes back to work.
            state["called_when_withheld"] = True
            return HookDecision()

        payload = parse_ask_user_args(
            getattr(mine[0], "arguments", None),
            max_questions=self._max_questions,
            max_outline_items=self._max_outline_items,
        )
        if len(proposed) > 1:
            # Mixed with other calls. Under a handoff the short circuit discards
            # the whole response by construction, so the rest cannot be kept
            # without leaving dangling tool_calls; asking wins and the loss is
            # counted. Under a round trip nothing is discarded - the key then
            # only says the ask shared its response with other calls.
            state["mixed_call"] = True
        if not payload.questions:
            # Guardrail 1: an outline alone never ends a turn. A question has a
            # threshold only the user can clear; an outline has none, so a model
            # that wants to look diligent can always produce one, and a handoff on
            # an outline would put a round trip in front of every research item.
            # NOT a halting decision - the loop continues and ``DRAskUserTool``'s
            # fallback string is what the model reads.
            state["outline_only_refused"] = bool(payload.outline)
            return HookDecision(notes=["ask_user: no answerable question, ignored"])

        if (
            self._delivery == "tool"
            and getattr(self._tool, "round_trip_ready", False)
        ):
            # The broker round trip: the questions reach the user as a structured
            # prompt, the answers return as the tool result, and the SAME turn
            # researches on them - no handoff, no pending, no chain debit, and no
            # ``clarify_requested`` marker because the turn still produces an
            # answer. The grant is read-once in the tool, so a call the gate did
            # not clear falls through to the undelivered fallback instead of
            # asking on its own.
            state.update({
                "asked": True,
                "delivery": "tool",
                "n_questions": len(payload.questions),
                "has_outline": bool(payload.outline),
                "n_outline": len(payload.outline),
            })
            self._tool.grant_round_trip()
            logger.info(
                "ask_user: broker round trip with %d question(s)", len(payload.questions)
            )
            return HookDecision(notes=["ask_user: broker round trip"])

        payload.original_question = own_text(ctx.turn_question or "")
        payload.chain_round = chain_round() + 1
        state.update({
            "asked": True,
            "delivery": "handoff",
            "n_questions": len(payload.questions),
            "has_outline": bool(payload.outline),
            "n_outline": len(payload.outline),
            "chain_round": payload.chain_round,
        })
        # C3's commit marker: the loop consults this instead of re-deriving "did
        # this turn produce an answer" from the text, the same discipline
        # ``salvage_committed`` states for itself. The handoff carries no closing
        # think tag, so every recomputation says "answerless" and fires a salvage
        # over the questions we just asked.
        ctx.metadata["clarify_requested"] = True
        # Which channel asked, not just that one did. This is the tool path, the
        # only one that stashes a pending, so it is the only one where
        # ``awaiting_user`` implies an open chain that ``maxRounds`` accounts for.
        ctx.metadata["clarify_source"] = "tool"
        stash_pending_clarify(payload.to_metadata())
        logger.info("ask_user: handing the turn back with %d question(s)", len(payload.questions))
        return HookDecision(short_circuit_result=render_handoff(payload))



def is_prose_clarify(ctx: AgentHookContext) -> bool:
    """Whether this terminal response is a clarify the model wrote as text.

    Structural, and nothing here reads the prose as language: the contract
    mandated a clarify this turn, no tool ran, this is the first iteration, the
    draft carries NO report section at all, and it asks something.

    ``missing == REPORT_SECTIONS``, not "missing anything". The first version of
    this check was ``bool(shape.missing)`` - i.e. "the bar would bounce it" -
    which exempted a real draft that had merely dropped ``## Limitations`` and so
    handed the reviewer's whole stratum a pass. The stated reason for the check
    only ever supported the strict form: a clarify has no report headings and a
    fabricated report has all three.

    The question mark carries the rest. Without it a first-turn answer written
    from memory in plain prose - no headings, no retrieval - is exempt, and that
    is precisely the draft the reviewer exists to catch. One character of
    punctuation, in both scripts, is not the "detect a phrase in model output"
    path the prompt detectors are forbidden from taking: nothing here appears in
    anything the model reads.

    Scoped to ``ask_required``, so only ``mode="first_turn"`` has an exempt turn
    at all. That scoping is load-bearing, not just conservative: the prose path
    stashes no pending, so nothing debits ``maxRounds`` for it, and a wider scope
    would let a model hand the turn away in prose on every turn of a session.
    """
    state = ctx.metadata.get("ask_user") or {}
    if not state.get("ask_required"):
        return False
    if getattr(ctx.response, "has_tool_calls", False):
        return False
    if (ctx.iteration or 1) != 1:
        return False
    content = getattr(ctx.response, "content", None) or ""
    if not content.strip():
        # An answerless terminal is ForcedFinalizeGate's, and it is not a clarify.
        # Also what makes this safe to call from ``terminal_answerless``, where the
        # loop sets ``ctx.response`` to None.
        return False
    if len(ReportShape(content).missing) != len(REPORT_SECTIONS):
        return False
    return bool(_QUESTION_MARK_RE.search(content))


class ClarifyExemptHook(AgentHook):
    """Forwards to a terminal gate, except on the response that IS the clarify.

    The tool path needs none of this: ``before_execute_tools`` short-circuits and
    the loop breaks, so ``after_iteration`` is structurally unreachable and no
    terminal gate ever sees a handoff. The PROSE path has no such protection. A
    model that writes its questions as text instead of calling the tool produces
    an ordinary text-only response, and the terminal gates read it as a draft.

    Observed once, and it reversed the product behaviour end to end: a first-turn
    clarify offering four senses of "Transformer" plus an outline was rejected by
    the reviewer for three "unsupported claims" and for "asking a clarifying
    question instead of providing the requested report". The bounce is persisted
    as a user message, so the session then carried an explicit statement that
    asking is a rejected draft, and the model went and researched the ambiguous
    question under an assumption.

    A wrapper rather than a branch inside the two gates, for ``GatedHook``'s
    reasons: those gates are the measured surface, and a wrapper applied only when
    ``askUser`` is on makes the off-state provable at one seam.

    The exemption is deliberately narrow. "Skip review on a mandated turn" would
    let the worst thing the reviewer exists to catch - a full report written from
    memory with no retrieval at all - ship unread on the commonest turn shape
    there is. See ``is_prose_clarify`` for the four conditions.
    """

    def __init__(self, inner: AgentHook) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return f"ClarifyExempt({self._inner.name})"

    @property
    def inner(self) -> AgentHook:
        return self._inner

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_user_inbound(ctx)

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_iteration(ctx)

    async def before_execute_tools(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.before_execute_tools(ctx)

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        if is_prose_clarify(ctx):
            # The commit marker the tool path already sets, and the reason this
            # wrapper does not need a fourth copy: the loop reads it for
            # ``answerless``, ``answerless_shape_exempt``, ``awaiting_user`` and the
            # invariants stamp, and ``terminal_answerless`` only fires when
            # ``answerless`` - so setting it closes that seam too. It does NOT close
            # the seam above it: ForcedFinalizeGate.after_iteration reads nothing
            # about a clarify, which is why that gate is wrapped as well.
            ctx.metadata["clarify_requested"] = True
            # The prose path stashes NO pending, so "awaiting_user is true" does
            # not imply an open chain here and ``maxRounds`` does not account for
            # this round - see ``is_prose_clarify`` on why the scope stays that
            # narrow. Stamped so a reader of ``turn_end`` alone can tell the two
            # apart; without it the key means two different things at once.
            ctx.metadata["clarify_source"] = "prose"
            # Recorded, because a suppression that leaves no trace reads exactly
            # like a gate that ran and found nothing. This is also the column that
            # makes ``asked`` readable: the model clarified and used the wrong
            # channel, which is a different failure from declining to clarify.
            state = ctx.metadata.setdefault("ask_user", {})
            state["prose_clarify"] = True
            state["clarify_chars"] = len(getattr(ctx.response, "content", None) or "")
            state["exempted"] = ",".join(
                n for n in (state.get("exempted", ""), self._inner.name) if n
            )
            return HookDecision()
        return await self._inner.after_iteration(ctx)

    async def terminal_answerless(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.terminal_answerless(ctx)

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        return await self._inner.after_send(ctx)


__all__ = [
    "BRIEF_CLOSE",
    "BRIEF_OPEN",
    "AskUserGate",
    "ClarifyExemptHook",
    "PendingClarify",
    "chain_round",
    "clarify_verdict",
    "is_first_turn",
    "is_reply_to",
    "is_prose_clarify",
    "own_text",
    "parse_ask_user_args",
    "render_brief",
    "render_handoff",
    "reply_overlap",
    "scaffold_language",
    "set_chain_round",
    "set_clarify_verdict",
    "set_first_turn",
    "set_turn_brief",
    "stash_pending_clarify",
    "strip_brief",
    "take_pending_clarify",
    "turn_brief",
]
