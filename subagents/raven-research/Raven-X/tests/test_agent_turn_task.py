"""The task a flow gate judges against — see ``raven/agent/flow/turn_task.py``.

Reproduced end to end on 2026-08-17: a follow-up turn had its correct answer
rejected and rewritten into an answer to the FIRST turn's question. These pin
both halves of the fix and, as importantly, pin the claim that the benchmark
path is unaffected by the half that would otherwise move published numbers.
"""

from __future__ import annotations

from raven.agent.context import ContextBuilder
from raven.agent.flow.conversation import MEMO_CLOSE, MEMO_OPEN
from raven.agent.flow.turn_task import task_for, turn_question, user_question
from raven.agent.hook.base import AgentHookContext

TAG = ContextBuilder._RUNTIME_CONTEXT_TAG
RUNTIME = f"{TAG}\nCurrent Time: 2026-08-17 08:55 (Monday) (UTC)\nChannel: cli\nChat ID: direct"

Q1 = "Serper.dev 的搜索 API 每 1000 次查询多少钱?"
Q2 = "那 Jina Reader 呢?"


def _turn(question: str, *, envelope: bool = True) -> dict:
    return {"role": "user", "content": f"{RUNTIME}\n\n{question}" if envelope else question}


# ── the reported bug ──────────────────────────────────────────────────────


def test_follow_up_turn_is_the_task_not_the_first_turn():
    """The defect verbatim: turn two must not be judged against turn one."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": Q1},  # persisted history, envelope stripped on save
        {"role": "assistant", "content": "$0.30 …"},
        _turn(Q2),
    ]
    assert turn_question(messages) == Q2


def test_third_turn_reads_the_third_question():
    """Not just "not the first" — the newest one, with history in between."""
    messages = [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": Q2},
        {"role": "assistant", "content": "a2"},
        _turn("那 Firecrawl 呢?"),
    ]
    assert turn_question(messages) == "那 Firecrawl 呢?"


def test_revision_prompt_cannot_become_the_task():
    """Why the value is captured at loop entry rather than re-derived.

    A rejected draft appends an assistant draft plus a user-role revision
    prompt. Reading "the last user message" at review time would hand the gate
    its own instruction text as the question.
    """
    entry = [{"role": "system", "content": "sys"}, _turn(Q2)]
    captured = turn_question(entry)
    after_rejection = [
        *entry,
        {"role": "assistant", "content": "draft about Jina"},
        {"role": "user", "content": "A reviewer rejected the draft above. Fix exactly …"},
    ]
    assert captured == Q2
    # The list can no longer answer the question; the captured value still can.
    assert turn_question(after_rejection) != Q2
    assert task_for(AgentHookContext(session_key="k", turn_question=captured, messages=after_rejection)) == Q2


# ── envelope stripping (each direction) ───────────────────────────────────


def test_runtime_context_envelope_is_removed():
    assert user_question(f"{RUNTIME}\n\n{Q1}") == Q1


def test_research_memo_is_removed():
    memo = f"{MEMO_OPEN}\nPages already opened: https://x.test\n{MEMO_CLOSE}"
    assert user_question(f"{memo}\n{RUNTIME}\n\n{Q2}") == Q2


def test_recovery_notice_in_front_does_not_defeat_the_stripper():
    """The regression the ``startswith`` form would have shipped."""
    recovery = "[Recovery — the previous turn was interrupted before finishing]\nCheckpoint: abc"
    assert user_question(f"{recovery}\n\n{RUNTIME}\n\n{Q1}") == Q1


def test_bare_question_is_returned_unchanged():
    """No envelope to strip must not mean no question."""
    assert user_question(Q1) == Q1


def test_envelope_only_message_yields_nothing():
    """Metadata alone must never be passed off as a question."""
    assert user_question(RUNTIME) == ""


def test_multimodal_blocks_keep_the_question_and_drop_the_envelope():
    content = [
        {"type": "text", "text": RUNTIME},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "text", "text": Q1},
    ]
    assert user_question(content) == Q1


def test_unknown_content_shape_is_empty_not_an_exception():
    assert user_question(None) == ""
    assert user_question(42) == ""


# ── the benchmark path must not move ─────────────────────────────────────


def test_single_turn_reads_the_only_question():
    """Every bench trajectory is one turn: first-user and last-user coincide.

    This is what makes the multi-turn half of the fix inert on published
    readings — the two derivations return the same string.
    """
    messages = [{"role": "system", "content": "sys"}, _turn(Q1)]
    first_user = next(m for m in messages if m.get("role") == "user")
    assert turn_question(messages) == user_question(first_user["content"]) == Q1


# ── fallback for contexts built outside the loop ──────────────────────────


def test_fallback_uses_the_first_user_message_when_nothing_was_captured():
    """Hooks driven directly (tests, embedders) still get a usable task."""
    ctx = AgentHookContext(
        session_key="k",
        messages=[{"role": "user", "content": f"{RUNTIME}\n\n{Q1}"}],
    )
    assert ctx.turn_question is None
    assert task_for(ctx) == Q1


def test_captured_value_wins_over_the_message_list():
    ctx = AgentHookContext(
        session_key="k",
        turn_question=Q2,
        messages=[{"role": "user", "content": Q1}],
    )
    assert task_for(ctx) == Q2


def test_task_is_empty_when_there_is_no_user_message_at_all():
    assert task_for(AgentHookContext(session_key="k", messages=[{"role": "system", "content": "s"}])) == ""
    assert task_for(AgentHookContext(session_key="k")) == ""
