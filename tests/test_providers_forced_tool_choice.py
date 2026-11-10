"""A refused forced tool choice costs one retry, not the call.

A vendor can accept a tool list and still reject being told it must call one:
DeepSeek's thinking mode answers a 400 with "Thinking mode does not support
this tool_choice". Every caller that forces a choice already handles a model
that declines under ``auto``, so the adapter relaxes and retries once rather
than returning the call as an error.

The shared retry ladder carries the same repair for every wire and reads the
refusal off the live exception. Nothing is remembered between calls.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from raven.providers import forced_tool_choice
from raven.providers import openai_responses_provider as mod
from raven.providers.base import LLMProvider, LLMResponse

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
    def __init__(self, *, forced: bool, status: int = 400) -> None:
        self.status_code = status if forced else 200

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


class TestOnlyA400:
    async def test_a_500_that_mentions_tool_choice_is_not_relaxed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway can echo the request body into a 5xx; that is not a refusal.

        Relaxing there would silently drop the forced call on a transient
        upstream fault, and the caller would read the missing tool call as the
        model declining.
        """

        class _ServerError(_RefuseThenAccept):
            def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
                _ServerError.bodies.append(dict(kwargs.get("json") or {}))
                return _Response(forced=True, status=500)

        _ServerError.bodies = []
        monkeypatch.setattr(mod.httpx, "AsyncClient", _ServerError)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-v4-pro")

        out = await provider.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
            tool_choice="required",
        )

        assert [b.get("tool_choice") for b in _ServerError.bodies] == ["required"]
        assert out.finish_reason == "error"


class TestTheAnnotatorOffersRatherThanForces:
    """The memory writers ask for the tool; they do not demand it.

    A forced choice is refused outright by some vendors, and only one of the
    three transports carries the relax-and-retry above. Forcing there means
    the log is never written on that transport at all, while the prompt
    already says to call the tool and both callers have always handled a
    model that declines.
    """

    def test_neither_memory_writer_forces_a_tool_choice(self) -> None:
        import inspect

        from raven.memory_engine.consolidate import consolidator as mod

        for fn in (mod.MemoryStore.annotate, mod.MemoryStore.refresh_section):
            src = inspect.getsource(fn)
            assert 'tool_choice="required"' not in src, fn.__qualname__
            assert 'tool_choice="auto"' in src, fn.__qualname__

    async def test_a_declining_model_is_handled_rather_than_forced(self, tmp_path) -> None:
        """What `auto` costs, and that it costs only this."""

        seen: list[Any] = []

        class _Declines:
            def get_default_model(self) -> str:
                return "m"

            async def chat_with_retry(self, **kwargs: Any) -> Any:
                # Recorded, not asserted here: ``annotate`` catches everything
                # this raises, so an assertion inside would be swallowed and
                # the test could not fail on it.
                seen.append(kwargs.get("tool_choice"))
                return type("R", (), {"has_tool_calls": False, "content": "I would rather not", "tool_calls": []})()

        from raven.memory_engine import MemoryStore

        store = MemoryStore(tmp_path)
        ok = await store.annotate([{"role": "user", "content": "x"}], _Declines(), "m")

        assert seen == ["auto"]
        assert ok is False
        assert not store.history_file.exists() or store.history_file.read_text(encoding="utf-8") == ""


_FORCED = {"type": "function", "function": {"name": "t"}}
_TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
_ASK = [{"role": "user", "content": "hi"}]
_DIRECT = (
    "Error calling LLM (invalid_request@deepseek): BadRequestError: Thinking mode does not support this tool_choice"
)


class _GatewayRefusal(Exception):
    """A gateway's 400: a generic message, the upstream's words only in the body."""

    status_code = 400
    body = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {"raw": '{"error":{"message":"Thinking mode does not support this tool_choice"}}'},
        }
    }

    def __str__(self) -> str:
        return "Provider returned error"


class TestTheRefusalIsReadOffTheLiveException:
    def test_a_direct_refusal_is_one(self) -> None:
        assert forced_tool_choice.is_rejection(_DIRECT) is True

    def test_a_gateway_wrapped_refusal_is_one_although_its_message_names_nothing(self) -> None:
        exc = _GatewayRefusal()
        assert "tool_choice" not in str(exc)
        assert forced_tool_choice.is_rejection(exc, 400) is True
        assert LLMProvider.classify_error(exc).refuses_forced_tool_choice is True

    def test_a_known_status_other_than_400_is_never_one(self) -> None:
        assert forced_tool_choice.is_rejection(_DIRECT, 500) is False

    def test_a_status_the_adapter_rendered_into_its_error_counts_as_known(self) -> None:
        assert forced_tool_choice.is_rejection("HTTP 500: invalid_request echoed: tool_choice") is False
        assert forced_tool_choice.is_rejection("HTTP 400: does not support this tool_choice") is True

    async def test_the_upstream_error_override_keeps_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Filed under ``server`` by the adapter, and still a refusal the ladder can answer."""

        class _UpstreamError(_RefuseThenAccept):
            def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
                raise RuntimeError("upstream_error: 400 Bad Request: does not support this tool_choice")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _UpstreamError)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

        out = await provider.chat(_ASK, tools=_TOOLS, tool_choice="required")

        assert out.error_classification.category == "server"
        assert out.error_classification.refuses_forced_tool_choice is True

    async def test_the_upstream_error_override_still_drops_the_prompt_cache_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unchanged on purpose: acting on it switches caching off for the model, process-wide."""

        class _UpstreamError(_RefuseThenAccept):
            def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
                raise RuntimeError("upstream_error: 400 Bad Request: cache_control is not allowed")

        monkeypatch.setattr(mod.httpx, "AsyncClient", _UpstreamError)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")

        out = await provider.chat(_ASK, tools=_TOOLS)

        assert out.error_classification.category == "server"
        assert out.error_classification.refuses_prompt_cache is False


def _laddered(monkeypatch: pytest.MonkeyPatch, *, refuse_auto: bool = False) -> tuple[Any, list[Any]]:
    """A provider whose ``chat`` refuses a forcing choice, behind the real ladder."""
    sent: list[Any] = []

    async def chat(**kwargs: Any) -> LLMResponse:
        sent.append(kwargs["tool_choice"])
        if forced_tool_choice.is_forcing(kwargs["tool_choice"]) or refuse_auto:
            return LLMResponse(content=_DIRECT, finish_reason="error")
        return LLMResponse(content="ok")

    provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
    monkeypatch.setattr(provider, "chat", chat)
    return provider, sent


class TestTheLadderRelaxesForEveryWire:
    async def test_a_refused_force_is_resent_under_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, sent = _laddered(monkeypatch)

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice=_FORCED)

        assert sent == [_FORCED, "auto"]
        assert out.finish_reason != "error"

    async def test_nothing_is_remembered_between_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The refusal turns on the request, so the next call is forced again."""
        provider, sent = _laddered(monkeypatch)

        await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice=_FORCED)
        await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="required")

        assert sent == [_FORCED, "auto", "required", "auto"]

    async def test_the_choice_is_relaxed_at_most_once_per_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, sent = _laddered(monkeypatch, refuse_auto=True)

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice=_FORCED)

        assert sent == [_FORCED, "auto"]
        assert out.finish_reason == "error"

    async def test_a_choice_that_does_not_force_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider, sent = _laddered(monkeypatch, refuse_auto=True)

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="auto")

        assert sent == ["auto"]
        assert out.finish_reason == "error"

    async def test_a_500_that_mentions_tool_choice_is_not_relaxed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[Any] = []

        async def chat(**kwargs: Any) -> LLMResponse:
            sent.append(kwargs["tool_choice"])
            raise RuntimeError('HTTP 500: {"error":{"message":"invalid_request echoed: tool_choice required"}}')

        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
        monkeypatch.setattr(provider, "chat", chat)
        monkeypatch.setattr(provider, "_CHAT_RETRY_DELAYS", (0,))

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="required")

        assert "auto" not in sent
        assert out.finish_reason == "error"

    async def test_the_ladder_does_not_repeat_a_resend_the_adapter_already_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adapter relaxed and was refused again; a ladder resend would be that same request."""

        class _AlwaysRefuse(_RefuseThenAccept):
            def stream(self, _method: str, _url: str, **kwargs: Any) -> "_Response":
                _AlwaysRefuse.bodies.append(dict(kwargs.get("json") or {}))
                return _Response(forced=True)

        _AlwaysRefuse.bodies = []
        monkeypatch.setattr(mod.httpx, "AsyncClient", _AlwaysRefuse)
        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
        # Two attempts, so the ladder has a resend to make if it is going to.
        monkeypatch.setattr(provider, "_CHAT_RETRY_DELAYS", (0,))

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="required")

        # The second attempt starts from ``auto``: forcing again would only be
        # refused again, and relaxing again would repeat the adapter's resend.
        assert [b.get("tool_choice") for b in _AlwaysRefuse.bodies] == ["required", "auto", "auto"]
        assert out.finish_reason == "error"

    async def test_a_resend_for_another_repair_starts_from_auto_as_well(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The adapter's ``auto`` resend was refused for its cache marks; the stripped resend must not force again."""
        from raven.providers import prompt_cache
        from raven.providers.base import ErrorClassification

        # The cache repair remembers the model for the process; keep that in this test.
        monkeypatch.setattr(prompt_cache, "_SUPPRESSED", set())
        sent: list[Any] = []

        async def chat(**kwargs: Any) -> LLMResponse:
            sent.append(kwargs["tool_choice"])
            if len(sent) > 1:
                return LLMResponse(content="ok")
            return LLMResponse(
                content="HTTP 400: cache_control is not allowed",
                finish_reason="error",
                error_classification=ErrorClassification(
                    "invalid_request", refuses_prompt_cache=True, relaxed_forced_tool_choice=True
                ),
            )

        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
        monkeypatch.setattr(provider, "chat", chat)

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="required")

        assert sent == ["required", "auto"]
        assert out.finish_reason != "error"

    async def test_the_anthropic_adapter_reports_its_own_relaxation_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        from raven.providers import anthropic_messages_provider as anthropic

        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["tool_choice"]["type"])
            return httpx.Response(400, json={"error": {"message": "this model does not support this tool_choice"}})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            anthropic.httpx, "AsyncClient", lambda *_a, **_kw: real_client(transport=httpx.MockTransport(handler))
        )
        provider = anthropic.AnthropicMessagesProvider(
            api_key="k", api_base="https://api.anthropic.com/v1", default_model="claude-sonnet-5-5"
        )

        out = await provider.chat(_ASK, tools=_TOOLS, tool_choice=_FORCED)

        assert seen == ["tool", "auto"]
        assert out.finish_reason == "error"
        assert out.error_classification.refuses_forced_tool_choice is False
        assert out.error_classification.relaxed_forced_tool_choice is True

    async def test_a_failure_with_its_own_recovery_is_not_answered_by_relaxing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A gateway echoing the request into an overflow names the field without refusing it."""
        sent: list[Any] = []

        async def chat(**kwargs: Any) -> LLMResponse:
            sent.append(kwargs["tool_choice"])
            return LLMResponse(
                content='HTTP 400: context length exceeded; request was {"tool_choice": "required"}',
                finish_reason="error",
            )

        provider = mod.OpenAIResponsesProvider(api_key="k", default_model="deepseek/deepseek-flash")
        monkeypatch.setattr(provider, "chat", chat)

        out = await provider.chat_with_retry(messages=_ASK, tools=_TOOLS, tool_choice="required")

        assert out.error_classification.should_compress is True
        assert out.error_classification.refuses_forced_tool_choice is True
        assert sent == ["required"]
