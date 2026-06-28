"""Tests for the LLM gate that filters router candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from raven.memory_engine.skill_forge.gate import LLMGateFilter
from raven.memory_engine.skill_forge.types import RouterHit


@dataclass
class _Resp:
    content: str
    finish_reason: str = "stop"


class _StubProvider:
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


def _hit(qid: str, name: str, body: str = "", desc: str = "") -> RouterHit:
    return RouterHit(
        qualified_id=qid, name=name, content=body, score=0.5,
        meta={"description": desc, "source": qid.split("/", 1)[0]},
    )


# ----------------------------------------------------------------------


async def test_empty_candidates_returns_empty() -> None:
    gate = LLMGateFilter(_StubProvider("{}"))
    assert await gate.filter("any task", []) == []


async def test_selects_by_qualified_id() -> None:
    provider = _StubProvider(json.dumps({
        "plan": "use pdf gen",
        "skills": ["local/pdf-gen"],
    }))
    gate = LLMGateFilter(provider, max_select=2)
    hits = [
        _hit("local/pdf-gen", "pdf-gen", body="generate pdf"),
        _hit("local/weather", "weather", body="get weather"),
    ]
    out = await gate.filter("make a pdf report", hits)
    assert [h.qualified_id for h in out] == ["local/pdf-gen"]


async def test_empty_skills_list_is_valid_inject_nothing() -> None:
    provider = _StubProvider(json.dumps({"plan": "nothing fits", "skills": []}))
    out = await LLMGateFilter(provider).filter("task", [_hit("local/foo", "foo")])
    assert out == []


async def test_respects_max_select_truncation() -> None:
    provider = _StubProvider(json.dumps({
        "plan": "p", "skills": ["local/a", "local/b", "local/c"],
    }))
    gate = LLMGateFilter(provider, max_select=2)
    hits = [_hit(f"local/{n}", n) for n in ("a", "b", "c")]
    out = await gate.filter("task", hits)
    assert [h.qualified_id for h in out] == ["local/a", "local/b"]


async def test_unknown_id_in_response_silently_dropped() -> None:
    provider = _StubProvider(json.dumps({
        "plan": "p", "skills": ["local/known", "ghost/missing"],
    }))
    hits = [_hit("local/known", "known")]
    out = await LLMGateFilter(provider).filter("task", hits)
    assert [h.qualified_id for h in out] == ["local/known"]


async def test_provider_error_falls_back_to_top_n() -> None:
    """Infra failure ≠ deliberate empty. Falling back to legacy top-N
    keeps the prompt populated; returning [] would silently kill skill
    injection when the provider has a transient blip."""
    provider = _StubProvider(RuntimeError("network blip"))
    gate = LLMGateFilter(provider, legacy_top_k=2)
    hits = [_hit(f"local/{n}", n) for n in ("a", "b", "c", "d")]
    out = await gate.filter("task", hits)
    assert [h.qualified_id for h in out] == ["local/a", "local/b"]


async def test_unparseable_response_falls_back_to_top_n() -> None:
    provider = _StubProvider("garbage no json here")
    gate = LLMGateFilter(provider, legacy_top_k=1)
    hits = [_hit("local/a", "a"), _hit("local/b", "b")]
    out = await gate.filter("task", hits)
    assert [h.qualified_id for h in out] == ["local/a"]


async def test_think_block_stripped_before_parse() -> None:
    """Qwen3-style reasoning models emit <think>...</think> before JSON.
    The gate must tolerate it without falling back."""
    provider = _StubProvider(
        '<think>let me think...</think>\n'
        '{"plan": "p", "skills": ["local/a"]}'
    )
    out = await LLMGateFilter(provider).filter("task", [_hit("local/a", "a")])
    assert [h.qualified_id for h in out] == ["local/a"]


async def test_tools_block_present_when_tools_given() -> None:
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    gate = LLMGateFilter(provider)
    await gate.filter("task", [_hit("local/a", "a")], available_tools=["read_file", "exec"])
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "# Agent Tools" in prompt
    assert "read_file" in prompt
    assert "exec" in prompt


async def test_tools_block_absent_when_tools_none() -> None:
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    await LLMGateFilter(provider).filter("task", [_hit("local/a", "a")])
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "# Agent Tools" not in prompt
