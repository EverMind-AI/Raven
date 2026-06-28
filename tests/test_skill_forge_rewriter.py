"""Tests for the query rewriter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from raven.memory_engine.skill_forge.rewriter import (
    QueryRewriter,
    RewriteResult,
)


@dataclass
class _Resp:
    content: str
    finish_reason: str = "stop"


class _StubProvider:
    """Minimal LLMProvider stand-in.

    ``response`` is either a string to wrap into ``_Resp``, an ``_Resp``
    directly, or an ``Exception`` raised on call to simulate provider
    failures.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        if isinstance(self._response, BaseException):
            raise self._response
        if isinstance(self._response, _Resp):
            return self._response
        return _Resp(content=str(self._response))


# ----------------------------------------------------------------------


async def test_analyze_no_retrieval_short_circuits() -> None:
    provider = _StubProvider(json.dumps({"need_retrieval": False}))
    rewriter = QueryRewriter(provider)
    result = await rewriter.analyze("hello there")
    assert result == RewriteResult(need_retrieval=False)


async def test_analyze_returns_rewritten_query() -> None:
    provider = _StubProvider(json.dumps({
        "need_retrieval": True,
        "rewritten_query": "generate pdf reports",
    }))
    rewriter = QueryRewriter(provider)
    result = await rewriter.analyze(
        "could you please generate a pdf report from /home/user/data.csv"
    )
    assert result.need_retrieval is True
    assert result.rewritten_query == "generate pdf reports"


async def test_analyze_handles_code_fence_wrapping() -> None:
    provider = _StubProvider(
        '```json\n{"need_retrieval": true, "rewritten_query": "trim"}\n```'
    )
    result = await QueryRewriter(provider).analyze("verbose query")
    assert result.rewritten_query == "trim"


async def test_analyze_provider_error_defaults_to_retrieval() -> None:
    provider = _StubProvider(RuntimeError("provider boom"))
    result = await QueryRewriter(provider).analyze("something")
    # Safe fallback — never silently disable retrieval on infra failure.
    assert result.need_retrieval is True
    assert result.rewritten_query is None


async def test_analyze_bad_json_defaults_to_retrieval() -> None:
    provider = _StubProvider("not json at all")
    result = await QueryRewriter(provider).analyze("something")
    assert result.need_retrieval is True


async def test_analyze_empty_query_skips_retrieval() -> None:
    provider = _StubProvider(json.dumps({"need_retrieval": True}))
    result = await QueryRewriter(provider).analyze("   ")
    # No point calling the LLM on whitespace — and the agent has nothing
    # to retrieve for anyway.
    assert result.need_retrieval is False
    assert provider.calls == []


async def test_analyze_finish_reason_error_defaults_to_retrieval() -> None:
    provider = _StubProvider(_Resp(content="", finish_reason="error"))
    result = await QueryRewriter(provider).analyze("q")
    assert result.need_retrieval is True
