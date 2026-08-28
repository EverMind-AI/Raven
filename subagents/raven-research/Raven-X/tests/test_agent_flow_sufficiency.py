"""The first-round sufficiency gate: when it asks, and what it does with the answer.

The gate spends a model call inside every turn that opens a page, and the action it
can take - telling the turn its research is finished - is the one direction this repo
has measured going wrong: a four-line guidance block saying "correct runs are short"
was read as "commit early", and two same-config replicates differed by 5 items fixed
and 9 broken, 5 of the 9 previously correct. So the properties worth pinning are not
"does it parse a verdict" but the three that bound that risk:

  * it asks **once**, and only after the contract's grounding floor is paid;
  * every failure of the judge leaves the turn researching (fail-open direction);
  * the action is an appended note, so the draft still reaches ``verify``.
"""

from __future__ import annotations

import asyncio
import json

from raven.agent.flow.sufficiency import SufficiencyGate
from raven.agent.harness_text import SUFFICIENCY_PREFIX, TOOL_OUTPUT_ELIDED
from raven.agent.hook.base import AgentHookContext

_OK_PAGE = json.dumps({"url": "https://example.com/pricing", "content": "the plan costs $50/mo"})
_FAILED_PAGE = json.dumps({"error": "403 forbidden"})
_SERP = json.dumps({"results": [{"title": "Pricing", "url": "https://example.com/pricing"}]})


class _Response:
    has_tool_calls = True
    finish_reason = "stop"

    def __init__(self, content: str) -> None:
        self.content = content


class _Provider:
    """Records every judge call so a test can assert the gate did not ask."""

    def __init__(self, *replies: object, sleep_s: float = 0.0) -> None:
        self._replies = list(replies)
        self._sleep_s = sleep_s
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs) -> _Response:
        self.calls.append(kwargs)
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        reply = self._replies.pop(0) if self._replies else '{"sufficient": false}'
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, _Response):
            return reply
        return _Response(str(reply))


def _round(*, page: str = _OK_PAGE, searched: bool = True) -> list[dict]:
    messages: list[dict] = [{"role": "user", "content": "what does it cost?"}]
    if searched:
        messages.append({"role": "tool", "name": "web_search", "content": _SERP})
    messages.append({"role": "tool", "name": "web_fetch", "content": page})
    return messages


def _run(gate: SufficiencyGate, messages: list[dict], *, metadata: dict | None = None,
         turn_base: int = 0, iteration: int = 2, has_tool_calls: bool = True) -> dict:
    meta = {} if metadata is None else metadata
    response = _Response("")
    response.has_tool_calls = has_tool_calls
    ctx = AgentHookContext(
        session_key="t",
        iteration=iteration,
        messages=messages,
        response=response,
        metadata=meta,
        turn_base=turn_base,
    )
    asyncio.run(gate.after_iteration(ctx))
    return meta.get("sufficiency_gate", {})


def _fired(messages: list[dict]) -> bool:
    return SUFFICIENCY_PREFIX in (messages[-1].get("content") or "")


def test_a_sufficient_verdict_appends_the_note_to_the_newest_tool_result():
    """The action is a note on the body the model is already reading, not a tool removal.

    In-history is the only train-serve-safe channel (the runtime-context block is
    stripped on persist), and appending rather than replacing is what keeps the real
    page in front of the model while it drafts.
    """
    provider = _Provider('{"sufficient": true, "reason": "price is stated on the page"}')
    messages = _round()
    state = _run(SufficiencyGate(provider), messages)

    assert _fired(messages)
    assert messages[-1]["content"].startswith(_OK_PAGE)
    assert state["outcome"] == "sufficient" and state["fired"] is True
    assert state["reason"] == "price is stated on the page"
    assert state["searches"] == 1 and state["fetches_ok"] == 1


def test_the_judge_is_not_asked_before_a_page_has_been_opened():
    """A search listing is titles and links; there is nothing to call sufficient yet.

    Asserted on the provider rather than on the outcome: the cost this gate is judged
    on is calls made, so "did not fire" and "did not ask" are different claims.
    """
    provider = _Provider('{"sufficient": true}')
    messages = [{"role": "user", "content": "q"}, {"role": "tool", "name": "web_search", "content": _SERP}]
    state = _run(SufficiencyGate(provider), messages)

    assert provider.calls == []
    assert not _fired(messages)
    assert state == {"searches": 1, "fetches_ok": 0}


def test_a_failed_fetch_is_not_a_page_opened():
    """``fetch_result_ok`` is the predicate, so a 403 leaves the floor unpaid."""
    provider = _Provider('{"sufficient": true}')
    messages = _round(page=_FAILED_PAGE)
    state = _run(SufficiencyGate(provider), messages)

    assert provider.calls == []
    assert state["fetches_ok"] == 0


def test_a_pasted_url_can_be_judged_when_the_search_floor_is_zero():
    """``min_searches: 0`` is the surface where the user brought the page themselves."""
    provider = _Provider('{"sufficient": true, "reason": "the page answers it"}')
    messages = _round(searched=False)
    state = _run(SufficiencyGate(provider, min_searches=0), messages)

    assert len(provider.calls) == 1
    assert state["outcome"] == "sufficient" and _fired(messages)


def test_an_insufficient_verdict_writes_nothing():
    provider = _Provider('{"sufficient": false, "reason": "no second source"}')
    messages = _round()
    state = _run(SufficiencyGate(provider), messages)

    assert not _fired(messages)
    assert state["outcome"] == "insufficient" and state["fired"] is False


def test_every_judge_failure_leaves_the_turn_researching():
    """Fail-open direction, asserted per failure mode rather than in aggregate.

    A transport error, a truncated generation, empty content and an unparsed verdict
    reach the turn as the same thing - nothing written - and each is a separate path
    in ``_judge``, so a regression in one is invisible in a test that only covers
    another.
    """
    truncated = _Response('{"sufficient": true}')
    truncated.finish_reason = "length"
    errored = _Response('{"sufficient": true}')
    errored.finish_reason = "error"
    for reply in (
        RuntimeError("connection reset"),
        truncated,
        errored,
        _Response(""),
        "not json at all",
        '{"sufficient": "maybe"}',
    ):
        provider = _Provider(reply)
        messages = _round()
        state = _run(SufficiencyGate(provider, timeout_seconds=0.5, attempt_timeout_seconds=0.2), messages)

        assert not _fired(messages), reply
        assert state["outcome"] == "fail_open", reply


def test_a_stalled_attempt_retries_but_an_exhausted_budget_fails_open():
    """The two halves of ``verify``'s timeout shape, which this gate copies: a call
    stalled on a dead pooled connection never errors and never returns, and a fresh
    attempt completes in seconds - so a stall costs an attempt, not the verdict. Only
    running out of total budget fails open."""
    provider = _Provider(asyncio.TimeoutError(), '{"sufficient": true}')
    messages = _round()
    state = _run(SufficiencyGate(provider), messages)
    assert len(provider.calls) == 2
    assert state["outcome"] == "sufficient" and _fired(messages)

    stalling = _Provider(sleep_s=0.2)
    messages = _round()
    state = _run(
        SufficiencyGate(stalling, timeout_seconds=0.1, attempt_timeout_seconds=0.05), messages
    )
    assert len(stalling.calls) >= 1
    assert state["outcome"] == "fail_open" and not _fired(messages)


def test_the_judge_is_asked_once_per_turn():
    """The trigger is a floor, so it stays true; without the latch the gate re-asks
    on every later iteration of the same turn - the cost shape ``fetch_gate``'s
    once-per-firing notice exists to avoid."""
    provider = _Provider('{"sufficient": false}', '{"sufficient": true}')
    messages = _round()
    metadata: dict = {}
    _run(SufficiencyGate(provider), messages, metadata=metadata)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    _run(SufficiencyGate(provider), messages, metadata=metadata, iteration=3)

    assert len(provider.calls) == 1
    assert not _fired(messages)


def test_a_fail_open_leaves_a_retry_for_the_next_qualifying_iteration():
    """Only a real verdict latches. The latch used to be set before the judge ran,
    so a truncated first-round judge consumed the turn's only judgement - observed
    live as a six-source second round nobody judged. A fail-open leaves one retry."""
    truncated = _Response('{"sufficient": true}')
    truncated.finish_reason = "length"
    provider = _Provider(truncated, '{"sufficient": true, "reason": "r"}')
    messages = _round()
    metadata: dict = {}
    gate = SufficiencyGate(provider)
    state = _run(gate, messages, metadata=metadata)
    assert state["outcome"] == "fail_open" and not _fired(messages)

    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state = _run(gate, messages, metadata=metadata, iteration=3)
    assert len(provider.calls) == 2
    assert state["outcome"] == "sufficient" and _fired(messages)


def test_the_retry_is_capped_so_a_flaky_upstream_cannot_tax_every_iteration():
    """The other half of the latch change: without a cap, a permanently broken
    judge would be re-asked on every remaining iteration of the turn."""
    provider = _Provider("not json", "still not json", '{"sufficient": true}')
    messages = _round()
    metadata: dict = {}
    gate = SufficiencyGate(provider)
    _run(gate, messages, metadata=metadata)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    _run(gate, messages, metadata=metadata, iteration=3)
    messages.append({"role": "tool", "name": "web_fetch", "content": _OK_PAGE})
    state = _run(gate, messages, metadata=metadata, iteration=4)

    assert len(provider.calls) == 2
    assert state["attempts"] == 2
    assert not _fired(messages)


def test_an_earlier_turns_research_does_not_pay_this_turns_floor():
    """Scope is ``turn_base``, not the session.

    A conversation whose previous turn searched and fetched would otherwise satisfy
    the floor on this turn's first iteration, and the note it writes - "the pages you
    have opened" - would be about pages this turn never saw.
    """
    provider = _Provider('{"sufficient": true}')
    messages = _round() + [{"role": "user", "content": "and the other one?"},
                           {"role": "tool", "name": "web_search", "content": _SERP}]
    state = _run(SufficiencyGate(provider), messages, turn_base=3)

    assert provider.calls == []
    assert state == {"searches": 1, "fetches_ok": 0}


def test_the_judge_reads_this_turns_evidence_fenced_as_untrusted():
    """The judge's verdict decides control flow and its input is page content, so the
    pack is fenced. Elided bodies are skipped rather than packed: a judge shown a
    placeholder is being asked whether the harness's own sentence answers the task."""
    provider = _Provider('{"sufficient": true}')
    messages = _round()
    messages.insert(1, {"role": "tool", "name": "web_fetch", "content": TOOL_OUTPUT_ELIDED})
    _run(SufficiencyGate(provider), messages)

    user = provider.calls[0]["messages"][-1]["content"]
    assert "BEGIN UNTRUSTED" in user
    assert "the plan costs $50/mo" in user
    assert TOOL_OUTPUT_ELIDED not in user
    assert "what does it cost?" in user


def test_a_turn_that_is_already_writing_is_not_taxed():
    """No tool calls means the answer is being drafted; there is nothing to release."""
    provider = _Provider('{"sufficient": true}')
    messages = _round()
    _run(SufficiencyGate(provider), messages, has_tool_calls=False)

    assert provider.calls == []
    assert not _fired(messages)


def test_the_judge_call_is_pinned_to_a_deterministic_low_effort_shape():
    """temperature 0 and the effort knob, for the reason the conversation gate's
    ``gate_max_tokens`` docstring records: a reasoning model spends the budget on its
    think block first and returns empty content, which reads as an unparsed verdict."""
    provider = _Provider('{"sufficient": true}')
    _run(SufficiencyGate(provider, model="small", max_tokens=256, reasoning_effort="low"), _round())

    call = provider.calls[0]
    assert call["model"] == "small"
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 256
    assert call["reasoning_effort"] == "low"
    # 5s inside the default 30s attempt slice - the transport deadline must win
    # the race against wait_for so a hang raises something classifiable.
    assert call["timeout"] == 25.0


def test_an_unset_effort_knob_sends_no_effort_parameter_at_all():
    """``None`` means "leave the provider default", and only OMITTING the argument
    does that: ``chat_with_retry`` resolves an absent value through its sentinel to
    ``self.generation.reasoning_effort``, while an explicit ``None`` suppresses the
    parameter on the wire. Passing it unconditionally would make the documented
    default unreachable - the same distinction the verify gate carries."""
    provider = _Provider('{"sufficient": true}')
    _run(SufficiencyGate(provider, reasoning_effort=None), _round())

    assert "reasoning_effort" not in provider.calls[0]


# --------------------------------------------------------------------------
# Wiring: default-off, and unreachable from the anchor by construction
# --------------------------------------------------------------------------


def test_the_gate_is_absent_until_its_knob_is_set():
    """Flow-on alone must not install it. The knob is the only thing that does, which
    is what lets an arm's config say whether its trajectories could have been
    released early."""
    from raven.agent.flow.dr import build_dr_flow
    from raven.config.raven import DRFlowConfig

    assembly = build_dr_flow(
        DRFlowConfig(enabled=True), _Provider(), max_iterations=40, context_window_tokens=65_536
    )
    assert not any(isinstance(o, SufficiencyGate) for o in assembly.observers)

    config = DRFlowConfig(enabled=True)
    config.sufficiency.enabled = True
    config.sufficiency.min_fetches = 2
    assembly = build_dr_flow(config, _Provider(), max_iterations=40, context_window_tokens=65_536)
    gate = next(o for o in assembly.observers if isinstance(o, SufficiencyGate))
    assert gate._min_fetches == 2


def test_the_anchor_cannot_reach_the_gate_however_the_knob_is_written():
    """``build_dr_flow`` returns None with the flow off, so the flow-off arm builds no
    assembly at all - the construction-level isolation AGENTS.md 0.4 requires of any
    change that would otherwise flatter the treated arm."""
    from raven.agent.flow.dr import build_dr_flow
    from raven.config.raven import DRFlowConfig

    config = DRFlowConfig(enabled=False)
    config.sufficiency.enabled = True
    assert build_dr_flow(
        config, _Provider(), max_iterations=40, context_window_tokens=65_536
    ) is None
