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
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.verify import DraftReviewerGate  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


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


def test_the_reviewer_samples_greedily_unless_a_temperature_is_set():
    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer))
    assert reviewer.calls[0]["temperature"] == 0.0

    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer, temperature=1.0))
    assert reviewer.calls[0]["temperature"] == 1.0


def test_the_transport_deadline_knob_is_kept_but_never_sent():
    """``attemptHttpTimeoutSeconds`` stays on the model for twin parity and reaches the
    gate, but the call never carries ``timeout``: the trunk provider protocol has no
    such argument, and passing it raised TypeError inside the call so every review
    failed open the moment the knob was set - the sufficiency judge's own defect."""
    reviewer = _Reviewer()
    _review(DraftReviewerGate(reviewer, attempt_timeout_seconds=40.0))
    assert "timeout" not in reviewer.calls[0]

    reviewer = _Reviewer()
    gate = DraftReviewerGate(reviewer, attempt_timeout_seconds=40.0, attempt_http_timeout_seconds=35.0)
    _review(gate)
    assert "timeout" not in reviewer.calls[0]
    assert gate._attempt_http_timeout_seconds == 35.0


def test_the_knob_travels_from_config_to_the_gate(tmp_path):
    from research_flow.config import FlowConfig
    from research_flow.flow import ToolHandles, build_chain
    from research_flow.state import SessionStore

    class _Stub:
        async def chat_with_retry(self, **kwargs):  # pragma: no cover
            raise AssertionError("no provider call expected")

    def _build(config):
        return build_chain(
            config,
            _Stub(),
            max_iterations=40,
            context_window_tokens=65_536,
            tools=ToolHandles(),
            store=SessionStore(tmp_path),
        )

    config = FlowConfig(enabled=True)
    config.verify.reasoning_effort = "low"
    config.verify.attempt_http_timeout_seconds = 35.0
    config.verify.temperature = 1.0
    chain = _build(config)
    gate = next(o for o in chain if isinstance(o, DraftReviewerGate))
    assert gate._reasoning_effort == "low"
    assert gate._temperature == 1.0
    assert gate._attempt_http_timeout_seconds == 35.0

    chain = _build(FlowConfig(enabled=True))
    gate = next(o for o in chain if isinstance(o, DraftReviewerGate))
    assert gate._reasoning_effort is None
    assert gate._attempt_http_timeout_seconds is None


def test_the_review_and_the_highlights_move_to_the_digest_provider_only_when_asked(tmp_path):
    from research_flow.config import FlowConfig
    from research_flow.flow import ToolHandles, TurnFrame, build_chain
    from research_flow.state import SessionStore

    agent, digest = _Reviewer(), _Reviewer()

    def _build(config, side):
        return build_chain(
            config,
            agent,
            max_iterations=40,
            context_window_tokens=65_536,
            tools=ToolHandles(),
            store=SessionStore(tmp_path),
            side=side,
        )

    def _parts(chain):
        gate = next(o for o in chain if isinstance(o, DraftReviewerGate))
        page = next(o for o in chain if isinstance(o, TurnFrame))._page
        return gate._provider, gate._model, page._provider, page._model

    config = FlowConfig(enabled=True)
    config.final_shape.report_structure = True
    config.final_shape.report_depth = True
    config.final_shape.report_layout = "reader"
    config.report_page.enabled = True
    config.report_page.highlights = True
    assert _parts(_build(config, (digest, "flash"))) == (agent, None, agent, None)

    config.verify.on_digest_provider = True
    config.report_page.highlights_on_digest_provider = True
    assert _parts(_build(config, (digest, "flash"))) == (digest, "flash", digest, "flash")
    # No digest provider of its own: both stay on the agent's.
    assert _parts(_build(config, None)) == (agent, None, agent, None)


def test_a_captured_review_request_lands_beside_the_turns_work_and_only_when_asked(tmp_path):
    from raven.agent import workdir

    with workdir.bind(tmp_path):
        _review(DraftReviewerGate(_Reviewer()))
        assert not (tmp_path / ".review-requests").exists()
        _review(DraftReviewerGate(_Reviewer(), capture_requests=True))
    (saved,) = (tmp_path / ".review-requests").glob("review-*.json")
    request = json.loads(saved.read_text(encoding="utf-8"))
    assert "Draft answer:" in request["user"] and request["system"] and request["tools"]
    # The request carries the evidence the turn read: owner-only, like the state the flow keeps.
    assert saved.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".review-requests").stat().st_mode & 0o777 == 0o700
