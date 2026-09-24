"""A turn that dies still leaves its work in the session.

``_save_turn`` only ran on the happy path, so a cancelled or crashed turn lost
everything back to the question that started it -- the reader watched a page of
streamed work vanish on the next reload. These tests drive the real
``_process_message`` path with providers that die at chosen points and assert
what the JSONL keeps: the tail of the turn, a synthetic result for any tool
call left open, and one closing marker that says why the transcript stops.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.hook import AgentHook, HookDecision
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.agent.loop.recovery import RecoveryLimits
from raven.providers.base import ErrorClassification, LLMProvider, LLMResponse, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import AnswerlessTurnError, Origin, TurnRequest


class DyingProvider(LLMProvider):
    """Answers ``script`` in order; a BaseException entry is raised instead."""

    def __init__(self, script: list[Any]):
        super().__init__(api_key="test")
        self._script = list(script)

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _make_msg(content: str = "hello") -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="tui", chat_id="chat1", sender_id="user", chat_type=ChatType.DM),
        text=content,
    )


def _agent(workspace: Path, provider: LLMProvider) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=3),
        tools=ToolWiring(restrict_to_workspace=True),
    )


def _persisted(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "sessions" / "tui" / "chat1.jsonl"
    assert path.exists(), "the broken turn was not persisted at all"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if r.get("_type") != "metadata"]


@pytest.mark.asyncio
async def test_a_cancelled_turn_keeps_its_question_and_says_who_ended_it(workspace):
    agent = _agent(workspace, DyingProvider([asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_make_msg("do the thing"))

    msgs = _persisted(workspace)
    assert any(m.get("role") == "user" and "do the thing" in str(m.get("content")) for m in msgs)
    marker = msgs[-1]
    assert marker.get("turn_ended", {}).get("status") == "cancelled"
    assert marker.get("timestamp"), "the marker closes the fold, so it needs the wall clock"


@pytest.mark.asyncio
async def test_a_crash_that_escapes_the_loop_leaves_a_failure_marker(workspace, monkeypatch):
    """A provider error response fails the turn in the loop's own words (the
    tests below); what this guards is the class that ESCAPES -- a bug past the
    provider try, a dying context engine -- which used to take the whole turn's
    record with it."""
    agent = _agent(workspace, DyingProvider([LLMResponse(content="unused", finish_reason="stop")]))

    async def explode(*args: Any, **kwargs: Any):
        raise RuntimeError("the engine room flooded")

    monkeypatch.setattr(agent, "_run_agent_loop", explode)
    with pytest.raises(RuntimeError):
        await agent._process_message(_make_msg("hello"))

    marker = _persisted(workspace)[-1]
    assert marker.get("turn_ended", {}).get("status") == "failed"
    assert "the engine room flooded" in marker["turn_ended"].get("reason", "")


@pytest.mark.asyncio
async def test_an_open_tool_call_is_closed_before_the_history_is_stored(workspace):
    """An assistant tool call with no result is a history strict providers
    reject on the NEXT turn, so the rescue closes it with an honest synthetic
    result rather than storing a shape that poisons the session."""
    agent = _agent(
        workspace,
        DyingProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-a", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                ),
                asyncio.CancelledError(),
            ]
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await agent._process_message(_make_msg("look around"))

    msgs = _persisted(workspace)
    assert msgs[-1].get("turn_ended", {}).get("status") == "cancelled"
    calls = [c["id"] for m in msgs for c in (m.get("tool_calls") or [])]
    results = {str(m.get("tool_call_id")) for m in msgs if m.get("role") == "tool"}
    assert set(calls) <= results, "every stored tool call must have a stored result"


@pytest.mark.asyncio
async def test_a_finished_turn_writes_no_marker(workspace):
    agent = _agent(workspace, DyingProvider([LLMResponse(content="done", finish_reason="stop")]))
    out = await agent._process_message(_make_msg("hello"))
    assert out is not None
    assert all("turn_ended" not in m for m in _persisted(workspace))


class _StreamingThenDying(LLMProvider):
    """Streams ``chunks``, then raises -- the shape a stop or a dropped
    connection takes while the answer is already on the reader's screen."""

    def __init__(self, chunks: list[str], death: BaseException):
        super().__init__(api_key="test")
        self._chunks = chunks
        self._death = death

    async def chat(self, messages, **kwargs: Any):
        raise AssertionError("the streaming path is the one under test")

    async def chat_stream(self, **kwargs: Any):
        from raven.providers.base import ChatDelta

        for chunk in self._chunks:
            yield ChatDelta(content=chunk)
        raise self._death

    def get_default_model(self) -> str:
        return "stub"


async def _streamed_turn(workspace: Path, death: BaseException) -> None:
    agent = _agent(workspace, _StreamingThenDying(["half an ", "answer"], death))

    async def _sink(_text: str) -> None:
        return None

    await agent._process_message(_make_msg("do the long thing"), on_token_delta=_sink)


@pytest.mark.asyncio
async def test_a_cancelled_turn_files_its_question_once_with_what_had_streamed(workspace):
    """The question is on disk before the attempt starts, so the rescue must
    file the tail only -- a rescue that re-filed it left the session opening on
    the same question twice, and the model reading it that way."""
    with pytest.raises(asyncio.CancelledError):
        await _streamed_turn(workspace, asyncio.CancelledError())

    msgs = _persisted(workspace)
    users = [m for m in msgs if m.get("role") == "user"]
    assert len(users) == 1, f"the question was filed twice: {users}"
    assert users[0]["content"] == "do the long thing"
    assert any("half an answer" in str(m.get("content")) for m in msgs), "what streamed was lost"
    assert msgs[-1]["content"] == "(turn cancelled by the user)"


@pytest.mark.asyncio
async def test_a_failed_turn_files_its_question_once_with_what_had_streamed(workspace):
    with pytest.raises(RuntimeError):
        await _streamed_turn(workspace, RuntimeError("the socket went away"))

    msgs = _persisted(workspace)
    users = [m for m in msgs if m.get("role") == "user"]
    assert len(users) == 1, f"the question was filed twice: {users}"
    assert any("half an answer" in str(m.get("content")) for m in msgs), "what streamed was lost"
    assert msgs[-1]["turn_ended"]["status"] == "failed"
    assert "the socket went away" in msgs[-1]["content"]


# --- the other way a turn dies: an error response the ladder could not wait out ---


def _error_response(content: str, category: str = "network") -> LLMResponse:
    """A final error response: not retryable, so neither the provider's own
    ladder nor the loop's asks again and the script is spent in one call."""
    return LLMResponse(
        content=content,
        finish_reason="error",
        error_classification=ErrorClassification(category, retryable=False),
    )


def _agent_without_ladder(
    workspace: Path,
    provider: LLMProvider,
    *,
    hooks: list[AgentHook] | None = None,
    retry_after_output: bool = False,
) -> AgentLoop:
    """The loop with no error ladder, so even a retryable error response would
    end the turn on the first call instead of after 15/30/60 s of waiting."""
    limits = RecoveryLimits(llm_error_retry_delays=(), llm_retry_after_output=retry_after_output)
    return AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=3, empty_recovery=limits),
        tools=ToolWiring(restrict_to_workspace=True),
        host=HostWiring(hooks=list(hooks or [])),
    )


@pytest.mark.asyncio
async def test_a_model_call_the_loop_gives_up_on_fails_the_turn_and_leaves_the_marker(workspace):
    """The provider handed back an error response and the ladder is spent: the
    turn fails in the loop's own words, and the transcript ends on the marker a
    crash leaves -- not on the question alone, and not on the error text filed
    as the model's answer."""
    error = "Error calling LLM (first_byte_timeout@stub): no first byte after 5.0s"
    agent = _agent_without_ladder(workspace, DyingProvider([_error_response(error)]))

    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_make_msg("hello"))

    assert str(failed.value) == error
    msgs = _persisted(workspace)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[-1]["turn_ended"] == {"status": "failed", "reason": error}
    # The two readers are told apart: ``turn_ended.reason`` keeps the
    # provider's own account for whoever is diagnosing the failure, while the
    # text the model reads back next turn names the category only.
    assert msgs[-1]["content"] == (
        "(turn failed: The model sent nothing before the first-byte timeout expired (stub). "
        "The runtime log has the provider's own account.)"
    )
    assert "no first byte after 5.0s" not in msgs[-1]["content"]


@pytest.mark.asyncio
async def test_an_exhausted_empty_response_recovery_leaves_the_same_marker(workspace):
    """A turn whose model never sent a word fails like a model call the loop gave
    up on, so the transcript ends the same way: on a marker saying the turn
    failed and why, not on the question alone and not on a manufactured reply.

    The reason is the loop's own sentence rather than a vendor's, so the model's
    copy of it is that sentence -- there is no category to summarise.
    """
    agent = _agent_without_ladder(workspace, DyingProvider([LLMResponse(content="", finish_reason="stop")] * 8))

    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_make_msg("hello"))

    reason = str(failed.value)
    assert reason == "The model returned no content on 3 attempt(s) (prefill 0, post-tool nudge 0, plain retry 2)."
    msgs = _persisted(workspace)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[-1]["turn_ended"] == {"status": "failed", "reason": reason}
    assert msgs[-1]["content"] == f"(turn failed: {reason})"


@pytest.mark.asyncio
async def test_a_vendors_body_does_not_reach_the_model_through_the_marker(workspace):
    """The marker is history the model reads on its next turn. A vendor's auth
    body carries a masked key and an account URL, and both would be spent
    context and something to answer rather than the end of the turn."""
    vendor_body = (
        "AuthenticationError: OpenrouterException - No auth credentials found for key sk-or-v1-a1b2...ef90; "
        "see https://openrouter.ai/settings/keys"
    )
    error = f"Error calling LLM (auth@openrouter): {vendor_body}"
    agent = _agent_without_ladder(workspace, DyingProvider([_error_response(error)]))

    with pytest.raises(AnswerlessTurnError):
        await agent._process_message(_make_msg("hello"))

    marker = _persisted(workspace)[-1]
    assert "sk-or-v1" not in marker["content"]
    assert "openrouter.ai/settings" not in marker["content"]
    assert marker["content"] == (
        "(turn failed: The provider rejected the credentials (openrouter). "
        "The runtime log has the provider's own account.)"
    )
    assert marker["turn_ended"]["reason"] == error


@pytest.mark.asyncio
async def test_a_crash_leaves_a_bounded_reason_on_the_marker(workspace):
    """A crash's message is arbitrary -- a chained SDK trace, a whole HTTP body
    -- and it is filed into a session a model reads back. It is cut to the same
    ceiling the lane's own event uses, and the mark says it was cut."""
    from raven.spine.events import TURN_FAILURE_TEXT_MAX

    class _Boom(Exception):
        pass

    agent = _agent_without_ladder(workspace, DyingProvider([]))

    async def _explode(*args, **kwargs):
        raise _Boom("z" * 5000)

    agent._run_agent_loop = _explode

    with pytest.raises(_Boom):
        await agent._process_message(_make_msg("hello"))

    marker = _persisted(workspace)[-1]
    reason = marker["turn_ended"]["reason"]
    assert len(reason) == TURN_FAILURE_TEXT_MAX and reason.endswith("...")
    # Not a canonical model-call sentence, so the model's copy is the same text.
    assert marker["content"] == f"(turn failed: {reason})"


@pytest.mark.asyncio
async def test_a_failed_call_keeps_the_turns_tool_work(workspace):
    """The marker is filed from the attempt's own list, so the tool call the
    turn made and the result it got stay on disk ahead of it."""
    agent = _agent_without_ladder(
        workspace,
        DyingProvider(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="call-a", name="list_dir", arguments={"path": "."})],
                    finish_reason="tool_calls",
                ),
                _error_response("Error calling LLM (server@stub): 503"),
            ]
        ),
    )

    with pytest.raises(AnswerlessTurnError):
        await agent._process_message(_make_msg("look around"))

    msgs = _persisted(workspace)
    assert msgs[-1]["turn_ended"] == {"status": "failed", "reason": "Error calling LLM (server@stub): 503"}
    calls = [c["id"] for m in msgs for c in (m.get("tool_calls") or [])]
    results = [m for m in msgs if m.get("role") == "tool"]
    assert calls == ["call-a"] and [r["tool_call_id"] for r in results] == ["call-a"]
    assert "[interrupted]" not in str(results[0]["content"]), "the real listing was stored, not a synthetic close"


@pytest.mark.asyncio
async def test_a_hook_that_salvages_an_answerless_turn_keeps_it_a_finished_turn(workspace):
    class Salvage(AgentHook):
        async def terminal_answerless(self, ctx):
            return HookDecision(short_circuit_result="here is what I have")

    agent = _agent_without_ladder(
        workspace, DyingProvider([_error_response("Error calling LLM (network@stub): boom")]), hooks=[Salvage()]
    )

    out = await agent._process_message(_make_msg("hello"))

    assert out is not None and out[0] == "here is what I have"
    assert all("turn_ended" not in m for m in _persisted(workspace))


@pytest.mark.asyncio
async def test_a_half_answer_is_not_what_the_failed_call_reports(workspace):
    """A stall after words had streamed used to hand those words back as the error
    response's content, and the turn then needed a setting to tell it whether the
    content it held was a diagnosis or half a reply. The account of the failure is
    the reason; the half answer is filed where a half answer goes."""
    agent = _agent_without_ladder(
        workspace, _StreamingThenDying(["half an ", "answer"], TimeoutError()), retry_after_output=True
    )

    async def _sink(_text: str) -> None:
        return None

    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_make_msg("hello"), on_token_delta=_sink)

    reason = "Error calling LLM (network): TimeoutError"
    assert str(failed.value) == reason
    msgs = _persisted(workspace)
    assert msgs[-1]["turn_ended"]["reason"] == reason
    assert any(str(m.get("content")) == "half an answer" for m in msgs), "the words the reader saw are still filed"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after_output", [False, True])
async def test_a_providers_own_account_is_the_reason_whatever_the_retry_setting(workspace, retry_after_output):
    """The reason is read off the response rather than guessed at from a setting:
    an account that is not the canonical sentence used to be replaced, with
    retries after output on, by one about a reply that had started streaming."""
    account = "The upstream closed the stream before its first chunk."
    agent = _agent_without_ladder(
        workspace, DyingProvider([_error_response(account)]), retry_after_output=retry_after_output
    )

    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_make_msg("hello"))

    assert str(failed.value) == account


@pytest.mark.asyncio
async def test_a_cut_streams_canonical_account_survives_retries_after_output(workspace):
    """``stream_llm_call`` words the cut-stream account in the canonical shape, and
    the loop files that account as the reason whatever the retry setting says."""
    cut = "Error calling LLM (network): the model's reply was cut off by the connection after 30s"
    agent = _agent_without_ladder(workspace, DyingProvider([_error_response(cut)]), retry_after_output=True)

    with pytest.raises(AnswerlessTurnError) as failed:
        await agent._process_message(_make_msg("hello"))

    assert str(failed.value) == cut
