"""Unit tests for RoutineValidator (Sentinel Stage 1)."""

from __future__ import annotations

import pytest

from raven.proactive_engine.sentinel.predictor.routine_validator import (
    VALIDATOR_TOOL,
    RoutineValidator,
)
from raven.proactive_engine.sentinel.types import Routine
from raven.providers.base import LLMResponse, ToolCallRequest

# ── stubs ─────────────────────────────────────────────────────────────


class _StubProvider:
    """Minimal LLMProvider-shaped stub. Records call history; replays a
    queued response (or raises)."""

    def __init__(self, *, response: LLMResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):  # noqa: ANN003
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


def _ok_response(*, is_routine: bool, confidence: float, reason: str = "ok") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest(
                id="tc1",
                name="routine_validation",
                arguments={
                    "is_routine": is_routine,
                    "confidence": confidence,
                    "reason": reason,
                },
            )
        ],
        finish_reason="stop",
    )


def _routine(**kw) -> Routine:
    defaults = dict(
        id="dow1-h09-meeting",
        pattern="Tuesday 09:00-12:00 — meeting · standup",
        keywords=["meeting", "standup"],
        day_of_week=1,
        time_slot=(9, 12),
        occurrence_count=4,
    )
    defaults.update(kw)
    return Routine(**defaults)


_HISTORY = "\n".join(f"[2026-04-{d:02d} 09:30] standup meeting notes" for d in (7, 14, 21, 28))
_NOW = 1_700_000_000_000


# ── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_happy_path_returns_validation():
    provider = _StubProvider(response=_ok_response(is_routine=True, confidence=0.85, reason="4 Tuesdays"))
    v = RoutineValidator(provider, model="test-model")
    out = await v.validate(_routine(), _HISTORY, now_ms=_NOW)
    assert out is not None
    assert out.is_routine is True
    assert out.confidence == 0.85
    assert "Tuesday" in out.reason or "4" in out.reason
    assert out.validated_at_ms == _NOW
    # Check provider was called with our tool schema, not someone else's
    assert provider.calls[0]["tools"] == [VALIDATOR_TOOL]
    assert provider.calls[0]["model"] == "test-model"


@pytest.mark.asyncio
async def test_validate_no_tool_call_returns_none():
    """LLM returned text content instead of calling the tool — must fail safely."""
    provider = _StubProvider(response=LLMResponse(content="just chatter", tool_calls=[]))
    v = RoutineValidator(provider, model="m")
    assert await v.validate(_routine(), _HISTORY, now_ms=_NOW) is None


@pytest.mark.asyncio
async def test_validate_provider_raises_returns_none():
    """LLM call exception (network, timeout, etc.) must not propagate."""
    provider = _StubProvider(exc=RuntimeError("connection reset"))
    v = RoutineValidator(provider, model="m")
    assert await v.validate(_routine(), _HISTORY, now_ms=_NOW) is None


@pytest.mark.asyncio
async def test_validate_finish_reason_error_returns_none():
    provider = _StubProvider(response=LLMResponse(content="upstream 500", finish_reason="error"))
    v = RoutineValidator(provider, model="m")
    assert await v.validate(_routine(), _HISTORY, now_ms=_NOW) is None


@pytest.mark.asyncio
async def test_validate_malformed_args_returns_none():
    """Tool call with missing required field must return None, not raise."""
    bad = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="x", name="routine_validation", arguments={"confidence": 0.7})],
        finish_reason="stop",
    )
    provider = _StubProvider(response=bad)
    v = RoutineValidator(provider, model="m")
    assert await v.validate(_routine(), _HISTORY, now_ms=_NOW) is None


@pytest.mark.asyncio
async def test_validate_args_not_dict_returns_none():
    bad = LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="x", name="routine_validation", arguments=[1, 2, 3])],  # type: ignore[arg-type]
        finish_reason="stop",
    )
    provider = _StubProvider(response=bad)
    v = RoutineValidator(provider, model="m")
    assert await v.validate(_routine(), _HISTORY, now_ms=_NOW) is None


@pytest.mark.asyncio
async def test_validate_confidence_clamped_to_unit_range():
    """LLMs occasionally return out-of-range confidence; clamp instead of reject."""
    provider = _StubProvider(response=_ok_response(is_routine=True, confidence=1.7))
    v = RoutineValidator(provider, model="m")
    out = await v.validate(_routine(), _HISTORY, now_ms=_NOW)
    assert out is not None
    assert out.confidence == 1.0

    provider2 = _StubProvider(response=_ok_response(is_routine=False, confidence=-0.3))
    v2 = RoutineValidator(provider2, model="m")
    out2 = await v2.validate(_routine(), _HISTORY, now_ms=_NOW)
    assert out2 is not None
    assert out2.confidence == 0.0


@pytest.mark.asyncio
async def test_validate_truncates_long_history():
    """Long history should be tail-truncated before going to the LLM."""
    long_history = "x" * 50_000  # well beyond _HISTORY_MAX_CHARS
    provider = _StubProvider(response=_ok_response(is_routine=False, confidence=0.1))
    v = RoutineValidator(provider, model="m")
    await v.validate(_routine(), long_history, now_ms=_NOW)
    user_content = provider.calls[0]["messages"][1]["content"]
    # The injected history should be much shorter than the input
    assert len(user_content) < 12_000


@pytest.mark.asyncio
async def test_validate_reason_truncated_to_300_chars():
    long_reason = "因为" * 500
    provider = _StubProvider(response=_ok_response(is_routine=True, confidence=0.9, reason=long_reason))
    v = RoutineValidator(provider, model="m")
    out = await v.validate(_routine(), _HISTORY, now_ms=_NOW)
    assert out is not None
    assert len(out.reason) <= 300


@pytest.mark.asyncio
async def test_validate_nan_confidence_normalized_to_zero():
    """LLM returning NaN would slip through max/min clamping (Python's
    max(0.0, NaN) == NaN). Must be caught explicitly."""
    import math

    provider = _StubProvider(response=_ok_response(is_routine=True, confidence=math.nan))
    v = RoutineValidator(provider, model="m")
    out = await v.validate(_routine(), _HISTORY, now_ms=_NOW)
    assert out is not None
    assert out.confidence == 0.0
    assert math.isfinite(out.confidence)


@pytest.mark.asyncio
async def test_validate_inf_confidence_normalized_to_zero():
    """+inf / -inf are also non-finite and must be rejected before clamping."""
    import math

    for bad in (math.inf, -math.inf):
        provider = _StubProvider(response=_ok_response(is_routine=True, confidence=bad))
        v = RoutineValidator(provider, model="m")
        out = await v.validate(_routine(), _HISTORY, now_ms=_NOW)
        assert out is not None
        assert out.confidence == 0.0
