"""dr@3.4-askuser: the clarify round across the turn boundary - consume, persist, classify.

The session-side half of the fork's ``test_agent_flow_ask_user.py``: what the fork's
``AgentLoop`` did around the gate - ``_consume_pending_clarify``,
``_persist_pending_clarify``, ``_inject_research_brief`` and ``_decide_turn_mode`` -
which on this build is one hook, ``research_flow.flow.TurnFrame``, over one store,
``research_flow.state.SessionStore``. The gate and the tool that open a round are
asserted in ``test_agents_research_flow_ask_user.py``; the pure text surface both
stand on in ``test_agents_research_ask_user_text.py``.

[port] The carrier moved. A hook is handed a ``session_key`` and never the
``Session``, so the pending is a field on the plugin's own ``SessionRecord`` - one
JSON file per session under the plugin's state root - instead of
``session.metadata["dr_pending_clarify"]``. That is itself the point the fork's
"not the personalizer's field" test made, so that test is ported against the new
carrier rather than dropped.

[port] Three of the fork's surfaces have no counterpart here and their assertions are
dropped rather than weakened:

* ``terminal_state`` and ``turn_invariants``. The trunk kernel exports no trajectory
  and runs no invariant checker, so ``awaiting_user`` and the ``ask_user`` namespace
  have no stamp to reach. The one fact that survived the move - which channel asked -
  is stamped on ``ctx.metadata["clarify_source"]`` by both paths and is asserted in
  the sibling file;
* ``AgentLoop._process_message``'s personalizer branch. This plugin contributes hooks
  and tools and never names personalization, so there is no supersede to order, to
  warn about, or to read off a source;
* ``_save_turn``'s strip pass. The trunk persists whatever ``before_user_inbound``
  rewrote the inbound to and strips only its own runtime-context tag, so no
  brief-stripping seam exists on this build to assert against.

[port] Chinese INPUT is what exercises the language switch and is spelled with
``\\uXXXX`` escapes, the way ``ask_user.py`` spells its own CJK range.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import fields
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import AskUserConfig, FlowConfig  # noqa: E402
from research_flow.flow import _TURN_MODE, TurnFrame  # noqa: E402
from research_flow.gates import ask_user as ask_user_module  # noqa: E402
from research_flow.gates.ask_user import (  # noqa: E402
    BRIEF_CLOSE,
    BRIEF_OPEN,
    PendingClarify,
    chain_round,
    clarify_verdict,
    set_chain_round,
    set_clarify_verdict,
    set_first_turn,
    set_turn_brief,
    stash_pending_clarify,
    take_pending_clarify,
    turn_brief,
)
from research_flow.gates.conversation import (  # noqa: E402
    MEMO_OPEN,
    ResearchMemo,
    TurnMode,
    set_research_turn,
)
from research_flow.state import SessionRecord, SessionStore  # noqa: E402
from research_flow.support.ledger import set_ledger_dir  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402

_KEY = "cli:t"


# ---------------------------------------------------------------------------
# Fixtures and stand-ins
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_turn_state():
    """Every ContextVar this feature owns, plus the ledger switch, reset around each test.

    They are process-wide within one asyncio context, and pytest gives every test the
    same one - a test that left ``chain_round`` at 1 would silently turn the next
    test's first question into ``chain_exhausted``. That is the same inheritance bug
    the production code sets them unconditionally to avoid.

    [port] The ledger is a module global set through ``set_ledger_dir`` on this build
    rather than the fork's environment variable, and ``TurnFrame.after_send`` resolves
    it: left pointing at another test's directory it would fold a stranger's rows into
    this session's memo.
    """

    def _reset():
        set_research_turn(True)
        set_chain_round(0)
        set_turn_brief("")
        set_clarify_verdict(None)
        set_first_turn(False)
        take_pending_clarify()
        set_ledger_dir(None)

    _reset()
    yield
    _reset()


def _store(root: Path) -> SessionStore:
    return SessionStore(root / "research_flow")


def _frame(store: SessionStore, *, gate=None, **flow) -> TurnFrame:
    """One turn frame over one store. ``gate`` stands in for the conversation gate,
    which the frame is handed rather than building - the same split ``build_chain``
    makes, so a test can supply one that records or one that refuses to be called."""
    return TurnFrame(FlowConfig(enabled=True, **flow), store, gate)


def _scenario(fn):
    """Run a whole scenario inside ONE asyncio context.

    ``asyncio.run`` creates a fresh Task and a Task COPIES the current Context, so a
    ContextVar set inside one ``asyncio.run`` is invisible to the next call and to the
    caller. The frame's two turn-entry phases work precisely because they run in the
    same task, so a test that called ``asyncio.run`` per phase would be asserting
    against a topology this code never has.
    """
    return asyncio.run(fn())


def _inbound(text: str, *, session_key: str = _KEY) -> AgentHookContext:
    return AgentHookContext(session_key=session_key, inbound_content=text)


def _iteration(*, prior=(), question="the question", session_key: str = _KEY) -> AgentHookContext:
    """The loop's SECOND context of the turn.

    Built fresh rather than reusing the inbound one because that is what the loop
    does - one context per phase - so nothing a hook wrote on ``ctx.metadata`` at turn
    entry is readable here. Everything the frame carries from one phase to the next
    travels in a ContextVar instead, which is what these tests exercise.
    """
    messages = [*prior, {"role": "user", "content": question}]
    return AgentHookContext(
        session_key=session_key,
        turn_question=question,
        turn_base=len(prior),
        iteration=1,
        messages=messages,
    )


def _outbound(text: str, *, session_key: str = _KEY) -> AgentHookContext:
    return AgentHookContext(session_key=session_key, outbound_content=text)


def _q(text="which entity?", options=()):
    return {"question": text, "options": list(options)}


def _pending(**kw) -> dict:
    kw.setdefault("questions", [_q()])
    return PendingClarify(**kw).to_metadata()


# The user's own words, in Chinese, as escapes: the switch has to be exercised with
# real CJK input, and a raw literal is what the repo's one catalog exists to keep out
# of every other module.
_ZH_ORIGINAL = "\u54ea\u4e00\u5bb6\u8bfa\u57fa\u4e9a\u5b9e\u4f53\u7684\u8425\u6536\u66f4\u9ad8\uff1f"
_ZH_ASKED = "\u4f60\u6307\u7684\u662f\u624b\u673a\u4e1a\u52a1\u8fd8\u662f\u901a\u4fe1\u7f51\u7edc\u4e1a\u52a1\uff1f"
_ZH_NEW_REQUEST = (
    "\u7b97\u4e86\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u6b27\u76df"
    "\u65b0\u7535\u6c60\u6cd5\u89c4\u7684\u5408\u89c4\u622a\u6b62"
    "\u65e5\u671f\u90fd\u6709\u54ea\u4e9b\u3002"
)


# ---------------------------------------------------------------------------
# The three injections at turn entry
# ---------------------------------------------------------------------------


def test_the_brief_is_injected_outside_the_memo(tmp_path) -> None:
    """Order asserted at the injection site, not just assumed by the stripper.
    ``strip_memo`` is ``startswith``-anchored: if the brief were injected INSIDE the
    memo, the memo would never match and would be persisted."""
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            research_memo=ResearchMemo(
                turns=1,
                queries=["q"],
                sources=[{"url": "https://example.com/a", "chars": 10, "turn": 1}],
                opened=["https://example.com/a"],
            ).to_metadata(),
            pending_clarify=_pending(original_question="How many?"),
            chain_round=1,
        ),
    )
    frame = _frame(
        store,
        conversation={"enabled": True, "research_memo": True},
        ask_user={"enabled": True, "brief": True},
    )
    ctx = _inbound("the phone maker")
    decision = _scenario(lambda: frame.before_user_inbound(ctx))

    content = decision.modified_content
    assert content.startswith(BRIEF_OPEN)
    assert content.index(BRIEF_CLOSE) < content.index(MEMO_OPEN)


def test_no_brief_is_injected_when_the_contextvar_is_empty(tmp_path) -> None:
    """The injection is unconditional code on the turn-entry path, so the case that
    has to hold is the ordinary one: nothing opened a round, nothing is prepended."""
    store = _store(tmp_path)
    frame = _frame(
        store,
        conversation={"enabled": True},
        ask_user={"enabled": True, "brief": True},
    )
    ctx = _inbound("the question")
    decision = _scenario(lambda: frame.before_user_inbound(ctx))

    assert turn_brief() == ""
    assert BRIEF_OPEN not in (decision.modified_content or ctx.inbound_content)


# ---------------------------------------------------------------------------
# The pending round trip through the frame's two seams
# ---------------------------------------------------------------------------


def test_the_loop_persists_what_the_gate_stashed_and_clears_it(tmp_path) -> None:
    """``take_*`` clears as it reads. Without that, a later turn that asked nothing
    would re-persist the previous turn's pending and answer it a second time - the
    same reason ``take_turn_rows`` is read-once."""
    store = _store(tmp_path)
    frame = _frame(
        store,
        conversation={"enabled": True, "research_memo": False},
        ask_user={"enabled": True},
        final_shape={"record": False, "process_appendix": False},
    )

    async def run():
        stash_pending_clarify(PendingClarify(questions=[_q()], chain_round=1).to_metadata())
        await frame.after_send(_outbound("the answer"))
        # A second turn that asked nothing must not write one.
        await frame.after_send(_outbound("the answer", session_key="cli:quiet"))

    _scenario(run)

    asked = store.load(_KEY)
    assert asked.pending_clarify["chain_round"] == 1
    assert asked.pending_clarify["asked_at"]  # stamped by the frame, which has the clock
    assert asked.chain_round == 1
    assert store.load("cli:quiet").pending_clarify is None


def test_consuming_a_pending_sets_the_chain_round_and_renders_a_brief(tmp_path) -> None:
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(
                original_question="Which entity had higher revenue?",
                questions=[_q("Do you mean the phone maker or the network business?")],
                chain_round=1,
            ),
            chain_round=1,
        ),
    )
    frame = _frame(store, conversation={"enabled": True}, ask_user={"enabled": True, "brief": True})

    async def run():
        await frame.before_user_inbound(_inbound("the network business"))
        return chain_round(), turn_brief(), clarify_verdict()

    round_, brief, verdict = _scenario(run)
    # Popped, not read: exactly one turn may consume a pending.
    assert store.load(_KEY).pending_clarify is None
    # THROUGH the consume, not reset - else maxRounds > 1 is unreachable.
    assert round_ == 1
    assert brief.startswith(BRIEF_OPEN)
    assert "the network business" in brief
    assert verdict[0] == "answered"
    assert 0.0 <= verdict[1] <= 1.0


def test_the_brief_is_off_by_default_even_when_the_reply_is_an_answer(tmp_path) -> None:
    """A pending is popped by the message that immediately follows the handoff, so at
    injection time the block's two halves are the two most recent messages in the
    conversation - nothing a trimmer would have reached. The block adds salience, not
    facts, and its benefit is unproven, so the default is off and the verdict becomes a
    recorded column with nothing depending on it.

    ``maxRounds > 1`` is the case that would justify turning it on: a second round's
    brief carries the FIRST round's answers, which are no longer adjacent.
    """
    assert AskUserConfig().brief is False
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(
                original_question="Which entity had higher revenue?",
                questions=[_q("Do you mean the phone maker or the network business?")],
                chain_round=1,
            ),
            chain_round=1,
        ),
    )
    frame = _frame(store, conversation={"enabled": True}, ask_user={"enabled": True})

    async def run():
        await frame.before_user_inbound(_inbound("the network business"))
        return chain_round(), turn_brief(), clarify_verdict()

    round_, brief, verdict = _scenario(run)
    assert brief == ""
    # Still recorded, and the chain still passes through: only the injection is off.
    assert verdict[0] == "answered"
    assert round_ == 1


def test_a_new_request_discards_the_pending_and_closes_the_chain(tmp_path) -> None:
    """The round trip was a pure loss and the count goes with it, so the session's next
    research question starts from a full budget. No brief is injected: the user
    answered nothing."""
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(
                original_question=_ZH_ORIGINAL,
                questions=[_q(_ZH_ASKED)],
                chain_round=1,
            ),
            chain_round=1,
        ),
    )
    frame = _frame(store, conversation={"enabled": True}, ask_user={"enabled": True})

    async def run():
        await frame.before_user_inbound(_inbound(_ZH_NEW_REQUEST))
        return chain_round(), turn_brief(), clarify_verdict()

    round_, brief, verdict = _scenario(run)
    assert store.load(_KEY).pending_clarify is None
    assert round_ == 0
    assert brief == ""
    assert verdict[0] == "new_request"


def test_the_answer_check_can_be_switched_off(tmp_path) -> None:
    """``briefRequiresAnswerCheck: false`` means every message following a handoff is
    treated as its answer - the diagnostic/ablation arm."""
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(
                original_question=_ZH_ORIGINAL,
                questions=[_q(_ZH_ASKED)],
                chain_round=1,
            ),
            chain_round=1,
        ),
    )
    frame = _frame(
        store,
        conversation={"enabled": True},
        ask_user={"enabled": True, "brief": True, "brief_requires_answer_check": False},
    )

    async def run():
        await frame.before_user_inbound(_inbound(_ZH_NEW_REQUEST))
        return turn_brief(), clarify_verdict()

    brief, verdict = _scenario(run)
    assert brief.startswith(BRIEF_OPEN)
    assert verdict[0] == "answered"


def test_a_turn_with_no_pending_leaves_both_contextvars_at_zero(tmp_path) -> None:
    """Set on EVERY turn, including to nothing. A turn that inherited the previous
    chain's count would be refused a legitimate first question, and a turn that
    inherited a brief would prepend a stale Q/A pair to an unrelated question - both
    silently. Same rule ``set_prior_sources`` states for itself."""
    store = _store(tmp_path)
    frame = _frame(store, conversation={"enabled": True}, ask_user={"enabled": True})

    async def run():
        set_chain_round(7)
        set_turn_brief("stale")
        await frame.before_user_inbound(_inbound("a fresh question"))
        return chain_round(), turn_brief()

    assert _scenario(run) == (0, "")


def test_a_flow_off_turn_clears_them_too(tmp_path) -> None:
    """The clear happens BEFORE the conversation surface is consulted, so an arm
    without that surface cannot inherit either value."""
    store = _store(tmp_path)
    frame = _frame(store)

    async def run():
        set_chain_round(7)
        set_turn_brief("stale")
        await frame.before_user_inbound(_inbound("q"))
        return chain_round(), turn_brief()

    assert _scenario(run) == (0, "")


def test_a_fork_does_not_inherit_a_pending_clarify(tmp_path) -> None:
    """``SessionManager.fork`` mints the child a fresh key, and this build addresses a
    pending BY key - one JSON file per session - so a child cannot answer its parent's
    open question. Asserted rather than assumed: the day a fork carries the key over,
    this becomes a child answering a question nobody asked it.

    [port] The fork's version read ``fork``'s source for "the child's metadata is not
    copied", because the pending lived in ``session.metadata``. Here the carrier is the
    plugin's own store, so the same guarantee is a property of the key and can be run
    instead of read.

    [merged] The surface file carried a twin of this test (each ported from a
    different fork original); its non-overlapping assertions -- the fork
    lineage, the round budget -- live here now. One property, one pin.
    """
    from raven.session.manager import SessionManager

    sessions = SessionManager(tmp_path / "workspace")
    parent = sessions.get_or_create("cli:parent")
    parent.add_message("user", "compare the two")
    sessions.save(parent)

    store = _store(tmp_path)
    store.save(parent.key, SessionRecord(pending_clarify=_pending(), chain_round=1))

    child = sessions.fork(parent.key)
    assert child is not None
    assert child.key != parent.key
    assert child.metadata["parent_session_id"] == parent.key, "the lineage is recorded even so"
    assert store.load(child.key).pending_clarify is None
    assert store.load(child.key).chain_round == 0
    # The parent's round is untouched: forking is not a way to spend it.
    assert store.load(parent.key).chain_round == 1
    assert store.load(parent.key).pending_clarify is not None
    # The trunk resets its own interaction wait-state for the same reason.
    assert child.pending_clarification is None


def test_the_pending_carrier_is_not_the_personalizers_field() -> None:
    """``session.pending_clarification`` is consumed BEFORE the loop by a branch that
    assumes its own three fields: writing ours there would make it run
    ``extract_and_store_preference`` on our questions and then clear them. One field
    with two owners is C6 made worse, not solved.

    [port] The fork asserted this against ``AgentLoop._persist_pending_clarify``. On
    this build the frame writes the plugin's own ``SessionRecord.pending_clarify``, so
    the assertion is that neither the frame nor the gate names the trunk's field -
    which still exists, and still belongs to the personalizer.
    """
    from raven.session.manager import Session

    assert "pending_clarification" not in inspect.getsource(ask_user_module)
    persist = inspect.getsource(TurnFrame.after_send)
    assert "record.pending_clarify = pending" in persist
    assert "pending_clarification" not in persist

    ours = {f.name for f in fields(SessionRecord)}
    assert "pending_clarify" in ours
    assert "pending_clarification" not in ours
    assert "pending_clarification" in {f.name for f in fields(Session)}


# ---------------------------------------------------------------------------
# The answer turn is a research turn
# ---------------------------------------------------------------------------


def test_the_clarify_answer_turn_is_research_without_consulting_the_gate(tmp_path) -> None:
    """The turn that answers our questions is the turn the research was waiting on, so
    it may not be classified out of researching.

    The gate is told ``research=false`` for "a correction of tone or scope" and for a
    conversational message, and a clarify answer ("the second one, 2024, the EU
    market") is exactly that shape. Classified non-research the tools leave the schema
    and the model answers a never-researched question from memory, silently. Under
    ``mode="first_turn"`` this is not an edge case: turn one always asks, so turn two
    is always this turn.

    A gate that RAISES stands in for "was not consulted" - the assertion is that the
    decision never reaches it, not merely that the answer came back research.
    """
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(
                questions=[_q("which market?"), _q("which year?")],
                chain_round=1,
            ),
            chain_round=1,
        ),
    )

    class _ExplodingGate:
        async def decide(self, question, history):
            raise AssertionError("the gate must not be consulted on a clarify answer")

    frame = _frame(
        store,
        gate=_ExplodingGate(),
        conversation={"enabled": True},
        ask_user={"enabled": True},
    )
    prior = [
        {"role": "user", "content": "compare the two"},
        {"role": "assistant", "content": "which market? which year?"},
    ]

    async def run():
        await frame.before_user_inbound(_inbound("the EU market, 2024"))
        ctx = _iteration(prior=prior, question="the EU market, 2024")
        await frame.before_iteration(ctx)
        return _TURN_MODE.get()

    mode = _scenario(run)
    assert mode.research is True
    assert mode.source == "clarify_answer"


def test_an_unrelated_follow_up_still_goes_to_the_gate(tmp_path) -> None:
    """The carve-out is scoped to a verdict of ``answered``, not to "a pending
    existed". A message the overlap check reads as a NEW question has to keep its
    ordinary classification, or the exemption would make every later turn in a
    clarified conversation unconditionally research."""
    store = _store(tmp_path)
    store.save(
        _KEY,
        SessionRecord(
            pending_clarify=_pending(questions=[_q(_ZH_ASKED)], chain_round=1),
            chain_round=1,
        ),
    )
    seen: list[str] = []

    class _RecordingGate:
        async def decide(self, question, history):
            seen.append(question)
            return TurnMode(False, "gate")

    frame = _frame(
        store,
        gate=_RecordingGate(),
        conversation={"enabled": True},
        ask_user={"enabled": True},
    )
    prior = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]

    async def run():
        await frame.before_user_inbound(_inbound(_ZH_NEW_REQUEST))
        ctx = _iteration(prior=prior, question=_ZH_NEW_REQUEST)
        await frame.before_iteration(ctx)
        return _TURN_MODE.get()

    mode = _scenario(run)
    assert seen, "an unrelated follow-up must still be classified"
    assert mode.source == "gate"
    assert mode.research is False
