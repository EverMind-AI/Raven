"""The reviewer's reasoning-effort knob, and the sentinel trap it must not step on.

``chat_with_retry`` distinguishes an ABSENT ``reasoning_effort`` (resolved to the
provider's generation default) from an explicit ``None`` (parameter suppressed).
Every measured arm ran the absent form, so the knob's unset state has to keep the
call byte-identical - which means the wiring may only pass the parameter when the
knob is set. These tests pin the call shape on both sides of that line; get it
wrong and no test that checks verdicts would notice, because the reviewer answers
either way.
"""

from __future__ import annotations

import asyncio

from raven.agent.flow.verify import DraftReviewerGate
from raven.agent.hook.base import AgentHookContext


class _Reviewer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)

        class _R:
            content = '{"pass": true, "unsupported_claims": [], "issues": []}'
            finish_reason = "stop"

        return _R()


def _review(gate: DraftReviewerGate) -> None:
    class _Response:
        has_tool_calls = False
        content = "The answer is 42. Source: https://example.com"

    ctx = AgentHookContext(
        session_key="t",
        iteration=3,
        messages=[
            {"role": "user", "content": "what is the answer?"},
            {"role": "tool", "name": "web_fetch", "content": '{"content": "42"}'},
        ],
        response=_Response(),
        metadata={},
    )
    asyncio.run(gate.after_iteration(ctx))


def test_unset_effort_keeps_the_parameter_out_of_the_call():
    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer))

    assert len(reviewer.calls) == 1
    assert "reasoning_effort" not in reviewer.calls[0]


def test_a_configured_effort_reaches_the_reviewer_call():
    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer, reasoning_effort="low"))

    assert reviewer.calls[0]["reasoning_effort"] == "low"


def test_the_transport_deadline_is_sent_only_when_its_knob_is_set():
    """Same conditional shape as the effort knob, for the same reason: an unset
    knob keeps the reviewer call byte-identical to every measured arm. Set, the
    deadline reaches the wire verbatim so a hang raises a classifiable transport
    exception (connect vs read) before the wait_for cancellation - which names
    nothing - erases it. Not derived from the slice and not unconditional,
    because an early-surfaced stall can let the ladder answer inside the same
    slice: a verdict a cancelled attempt never could (AGENTS.md 0.2)."""
    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer, attempt_timeout_seconds=40.0))
    assert "timeout" not in reviewer.calls[0]

    reviewer = _Reviewer()
    _review(DraftReviewerGate(
        reviewer, attempt_timeout_seconds=40.0, attempt_http_timeout_seconds=35.0
    ))
    assert reviewer.calls[0]["timeout"] == 35.0


def test_the_knob_travels_from_config_to_the_gate():
    from raven.agent.flow.dr import build_dr_flow
    from raven.config.raven import DRFlowConfig

    class _Stub:
        async def chat_with_retry(self, **kwargs):  # pragma: no cover
            raise AssertionError("no provider call expected")

    config = DRFlowConfig(enabled=True)
    config.verify.reasoning_effort = "low"
    config.verify.attempt_http_timeout_seconds = 35.0
    assembly = build_dr_flow(config, _Stub(), max_iterations=40, context_window_tokens=65_536)
    gate = next(o for o in assembly.observers if isinstance(o, DraftReviewerGate))
    assert gate._reasoning_effort == "low"
    assert gate._attempt_http_timeout_seconds == 35.0

    assembly = build_dr_flow(
        DRFlowConfig(enabled=True), _Stub(), max_iterations=40, context_window_tokens=65_536
    )
    gate = next(o for o in assembly.observers if isinstance(o, DraftReviewerGate))
    assert gate._reasoning_effort is None
    assert gate._attempt_http_timeout_seconds is None
