"""A refused forced tool choice costs one retry, not the call.

A vendor can accept a tool list and still reject being told it must call one:
DeepSeek's thinking mode answers a 400 with "Thinking mode does not support
this tool_choice". Every caller that forces a choice already handles a model
that declines under ``auto``, so the adapter relaxes and retries once rather
than returning the call as an error.
"""

from __future__ import annotations

from typing import Any

import pytest

from raven.providers import openai_responses_provider as mod

pytestmark = pytest.mark.anyio

_REFUSAL = '{"error":{"message":"Thinking mode does not support this tool_choice"}}'
_SSE = (
    'data: {"type":"response.output_item.done","item":{"type":"function_call",'
    '"call_id":"c1","name":"annotate_conversation","arguments":"{\\"episode_summary\\":[]}"}}',
    'data: {"type":"response.completed","response":{"usage":{}}}',
    "data: [DONE]",
)


class _RefuseThenAccept:
    """400 on a forcing tool_choice, 200 once the body says ``auto``."""

    bodies: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_RefuseThenAccept":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
        body = kwargs.get("json") or {}
        _RefuseThenAccept.bodies.append(dict(body))
        return _Response(forced=body.get("tool_choice") != "auto")


class _Response:
    def __init__(self, *, forced: bool) -> None:
        self.status_code = 400 if forced else 200

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def aread(self) -> bytes:
        return _REFUSAL.encode()

    async def aiter_lines(self):
        for line in _SSE:
            yield line
            yield ""


class TestTheRelaxation:
    @pytest.mark.parametrize(
        "choice",
        ["required", "any", {"type": "function", "name": "emit"}, {"type": "tool", "name": "emit"}],
    )
    def test_a_forcing_choice_is_relaxed_when_the_refusal_names_it(self, choice: Any) -> None:
        body = {"tool_choice": choice}
        assert mod._relax_forced_tool_choice(body, "does not support this tool_choice") is True
        assert body["tool_choice"] == "auto"

    def test_auto_is_left_alone(self) -> None:
        body = {"tool_choice": "auto"}
        assert mod._relax_forced_tool_choice(body, "does not support this tool_choice") is False
        assert body["tool_choice"] == "auto"

    def test_a_refusal_about_something_else_is_not_ours(self) -> None:
        """Only the complaint that names it; a ceiling or a temperature is another repair's."""
        body = {"tool_choice": "required"}
        assert mod._relax_forced_tool_choice(body, "max_tokens is above the model limit") is False
        assert body["tool_choice"] == "required"


class TestTheRetry:
    async def test_a_refused_force_is_retried_once_under_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _RefuseThenAccept.bodies = []
        monkeypatch.setattr(mod.httpx, "AsyncClient", _RefuseThenAccept)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-v4-pro")

        out = await provider.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "annotate_conversation", "parameters": {}}}],
            tool_choice="required",
        )

        sent = [b.get("tool_choice") for b in _RefuseThenAccept.bodies]
        assert sent == ["required", "auto"], sent
        assert out.finish_reason != "error"
        assert [c.name for c in out.tool_calls] == ["annotate_conversation"]

    async def test_the_relaxation_happens_at_most_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 400 that keeps naming tool_choice must not loop."""

        class _AlwaysRefuse(_RefuseThenAccept):
            def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
                _AlwaysRefuse.bodies.append(dict(kwargs.get("json") or {}))
                return _Response(forced=True)

        _AlwaysRefuse.bodies = []
        monkeypatch.setattr(mod.httpx, "AsyncClient", _AlwaysRefuse)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-v4-pro")

        out = await provider.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_choice="required",
        )

        assert len(_AlwaysRefuse.bodies) == 2
        assert out.finish_reason == "error"
