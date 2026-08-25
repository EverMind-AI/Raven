"""Tests for the LLM gate that filters router candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
        qualified_id=qid,
        name=name,
        content=body,
        score=0.5,
        meta={"description": desc, "source": qid.split("/", 1)[0]},
    )


# ----------------------------------------------------------------------


async def test_empty_candidates_returns_empty() -> None:
    gate = LLMGateFilter(_StubProvider("{}"))
    assert await gate.filter("any task", []) == []


async def test_selects_by_qualified_id() -> None:
    provider = _StubProvider(
        json.dumps(
            {
                "plan": "use pdf gen",
                "skills": ["local/pdf-gen"],
            }
        )
    )
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
    provider = _StubProvider(
        json.dumps(
            {
                "plan": "p",
                "skills": ["local/a", "local/b", "local/c"],
            }
        )
    )
    gate = LLMGateFilter(provider, max_select=2)
    hits = [_hit(f"local/{n}", n) for n in ("a", "b", "c")]
    out = await gate.filter("task", hits)
    assert [h.qualified_id for h in out] == ["local/a", "local/b"]


async def test_unknown_id_in_response_silently_dropped() -> None:
    provider = _StubProvider(
        json.dumps(
            {
                "plan": "p",
                "skills": ["local/known", "ghost/missing"],
            }
        )
    )
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
    provider = _StubProvider('<think>let me think...</think>\n{"plan": "p", "skills": ["local/a"]}')
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


async def test_subagent_block_present_when_roster_given() -> None:
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    gate = LLMGateFilter(provider)
    await gate.filter(
        "task",
        [_hit("local/a", "a")],
        available_subagents="Raven-Research [stateless] (reads live web pages and cites them)",
    )
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "# Delegable Sub-Agents" in prompt
    assert "Raven-Research" in prompt
    assert "already covered by" in prompt


async def test_subagent_block_absent_when_no_roster() -> None:
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    await LLMGateFilter(provider).filter("task", [_hit("local/a", "a")])
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "# Delegable Sub-Agents" not in prompt
    assert "already covered by" not in prompt


async def test_plan_step_offers_delegation_as_an_alternative_to_running_the_tools() -> None:
    """The overlap check in step 2 cannot undo a plan step 1 already committed
    to. Asked only which tools to call, the gate plans the work inline and the
    candidate is then genuinely relevant to that plan -- so delegation has to be
    on the table while the plan is still forming. One sub-agent or several: a
    task split across a graph is delegated just as much as a single spawn."""
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    await LLMGateFilter(provider).filter(
        "task",
        [_hit("local/a", "a")],
        available_subagents="Raven-Research [stateless] (reads live web pages and cites them)",
    )
    prompt = provider.calls[0]["messages"][0]["content"]
    plan_step = prompt.split("1. **Plan**:")[1].split("2. **Filter**:")[0]
    assert "one or several of the sub-agents above" in plan_step
    # The roster sentence must not read as a one-agent-only offer either.
    assert "split it across several of them as a graph" in prompt


async def test_plan_step_is_unchanged_when_no_roster_is_supplied() -> None:
    """With nothing to delegate to, "the sub-agents above" names nothing, so the
    clause is withheld rather than left dangling."""
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    await LLMGateFilter(provider).filter("task", [_hit("local/a", "a")])
    prompt = provider.calls[0]["messages"][0]["content"]
    assert "1. **Plan**: briefly think about what the task requires and which sequence" in prompt
    assert "sub-agents above should carry it" not in prompt


async def test_an_unpaired_gate_model_is_never_sent() -> None:
    """``skill_forge.llm_gate_model`` without credentials of its own is a bare
    id. Forwarding it would post it on whatever key this provider holds, which
    is the mis-pairing the pool exists to prevent; the gate follows the turn
    instead and names no model."""
    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    gate = LLMGateFilter(provider, model="gpt-4o")
    await gate.filter("task", [_hit("local/a", "a")])
    assert provider.calls[0]["model"] is None


async def test_a_paired_gate_pin_is_used_as_given() -> None:
    """With the pin paired by the pool, both halves are the gate's own."""
    from raven.providers.binding import ModelBinding

    provider = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    pinned = _StubProvider(json.dumps({"plan": "p", "skills": []}))
    gate = LLMGateFilter(provider, model="gpt-4o", pin=ModelBinding(pinned, "gpt-4o"))
    await gate.filter("task", [_hit("local/a", "a")])
    assert provider.calls == []
    assert pinned.calls[0]["model"] == "gpt-4o"
