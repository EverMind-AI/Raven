"""The reviewer's verdict channel: a forced tool call first, text as the fallback.

Two of nine text replies on one measured draft carried no boolean ``pass`` and each
cost a fail-open. The gate now asks for the verdict as a function call, reads the
parsed arguments when they arrive, still parses content when a backend answers in
text, and names the way it failed when it fails open.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

import json

from research_flow.gates.verify import _VERDICT_TOOL_NAME, DraftReviewerGate  # noqa: E402
from research_flow.support import ledger as ledger_mod  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


class _Reviewer:
    """``chat_with_retry`` bound to the trunk protocol's parameters, kwarg for kwarg.

    A ``**kwargs`` stub would accept a kwarg the real provider rejects, and the gate's
    fail-open contract would then record the TypeError as an ordinary unavailable
    reviewer -- the shape that hid the sufficiency gate's ``timeout`` for a week. The
    forced tool call this file covers adds ``tools`` and ``tool_choice`` to the call,
    so the stub is the guard that they stay in the protocol.
    """

    def __init__(self, response) -> None:
        self.calls: list[dict] = []
        self._response = response

    async def chat_with_retry(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=None,
        temperature=None,
        reasoning_effort=None,
        tool_choice=None,
        fallback_models=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "tool_choice": tool_choice,
                "fallback_models": fallback_models,
            }
        )
        return self._response


def _ctx() -> AgentHookContext:
    return AgentHookContext(
        session_key="verdict",
        iteration=3,
        messages=[
            {"role": "user", "content": "what is the answer?"},
            {"role": "tool", "name": "web_fetch", "content": '{"content": "42"}'},
        ],
        response=SimpleNamespace(has_tool_calls=False, content="The answer is 42. Source: https://example.com"),
        metadata={},
    )


def _tool_reply(arguments: dict, finish_reason: str = "tool_calls", truncated: bool = False):
    call = SimpleNamespace(id="c1", name=_VERDICT_TOOL_NAME, arguments=arguments)
    return SimpleNamespace(content=None, tool_calls=[call], finish_reason=finish_reason, truncated=truncated)


def test_the_verdict_is_requested_as_a_forced_tool_call():
    reviewer = _Reviewer(_tool_reply({"pass": True, "unsupported_claims": [], "estimated_cells": [], "issues": []}))
    ctx = _ctx()
    asyncio.run(DraftReviewerGate(reviewer).after_iteration(ctx))
    call = reviewer.calls[0]
    assert [t["function"]["name"] for t in call["tools"]] == [_VERDICT_TOOL_NAME]
    assert call["tool_choice"] == {"type": "function", "function": {"name": _VERDICT_TOOL_NAME}}
    assert ctx.metadata["verify_gate"]["passes"] == 1


def test_a_tool_verdict_rejects_and_names_the_claim():
    reviewer = _Reviewer(
        _tool_reply({"pass": False, "unsupported_claims": ["the 42"], "estimated_cells": [], "issues": ["x"]})
    )
    ctx = _ctx()
    decision = asyncio.run(DraftReviewerGate(reviewer, strict_reject_only=True).after_iteration(ctx))
    assert decision.rollback is True
    assert ctx.metadata["verify_gate"]["rejects"] == 1


def test_a_string_pass_in_the_arguments_is_coerced_like_text():
    reviewer = _Reviewer(_tool_reply({"pass": "false", "unsupported_claims": "the 42", "issues": None}))
    ctx = _ctx()
    asyncio.run(DraftReviewerGate(reviewer, strict_reject_only=True).after_iteration(ctx))
    verdict = ctx.metadata["verify_gate"]["verdict"]
    assert verdict["pass"] is False and verdict["unsupported_claims"] == ["the 42"]


def test_a_text_reply_still_parses_when_no_tool_call_arrives():
    reply = SimpleNamespace(
        content='{"pass": true, "unsupported_claims": [], "issues": []}', tool_calls=[], finish_reason="stop"
    )
    ctx = _ctx()
    asyncio.run(DraftReviewerGate(_Reviewer(reply)).after_iteration(ctx))
    assert ctx.metadata["verify_gate"]["passes"] == 1


def test_a_truncated_response_fails_open_and_says_so(tmp_path):
    ledger_mod.set_ledger_dir(tmp_path)
    ledger = Path(ledger_mod.open_product_ledger("verdict"))
    try:
        reply = SimpleNamespace(content=None, tool_calls=[], finish_reason="stop", truncated=True)
        ctx = _ctx()
        asyncio.run(DraftReviewerGate(_Reviewer(reply)).after_iteration(ctx))
        state = ctx.metadata["verify_gate"]
        assert state["fail_open"] == 1 and state["fail_open_reason"] == "truncated"
        rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
        rows = [r for r in rows if r.get("op") == "verify"]
        assert rows and rows[-1]["outcome"] == "unavailable" and rows[-1]["fail_open_reason"] == "truncated"
    finally:
        ledger_mod.close_product_ledger()
        ledger_mod.set_ledger_dir(None)


def test_prose_without_a_verdict_fails_open_as_unparsed():
    reply = SimpleNamespace(content="I think the draft is fine.", tool_calls=[], finish_reason="stop")
    ctx = _ctx()
    asyncio.run(DraftReviewerGate(_Reviewer(reply)).after_iteration(ctx))
    assert ctx.metadata["verify_gate"]["fail_open_reason"] == "unparsed"


def test_a_stalled_budget_fails_open_as_timeout():
    class _Hang(_Reviewer):
        async def chat_with_retry(self, *args, **kwargs):
            await super().chat_with_retry(*args, **kwargs)
            await asyncio.sleep(5)

    ctx = _ctx()
    asyncio.run(DraftReviewerGate(_Hang(None), timeout_seconds=0.05, attempt_timeout_seconds=0.05).after_iteration(ctx))
    assert ctx.metadata["verify_gate"]["fail_open_reason"] == "timeout"


def test_a_verdict_call_without_a_usable_pass_is_named_as_such():
    """A provider that hands the arguments back as a string, or a model that omits the
    one required boolean, answered on the tool channel; the ledger must not call that
    an empty reply, because the cure is the schema or the argument parser."""
    call = SimpleNamespace(id="c1", name=_VERDICT_TOOL_NAME, arguments='{"pass": false}')
    reply = SimpleNamespace(content=None, tool_calls=[call], finish_reason="tool_calls")
    ctx = _ctx()
    asyncio.run(DraftReviewerGate(_Reviewer(reply)).after_iteration(ctx))
    assert ctx.metadata["verify_gate"]["fail_open_reason"] == "tool_args_unusable"
