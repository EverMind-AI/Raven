"""The per-section `tls_verify` knob as the call clients read it.

covers:
- the schema accepts `tls_verify` (camelCase and snake_case) and defaults it on
- LiteLLMProvider(tls_verify=False) hands `acompletion` a prebuilt AsyncOpenAI
  built on an httpx client constructed with `verify=False`, on chat() and
  chat_stream(), one client across calls; the default keeps the `client` kwarg
  absent, so LiteLLM builds and verifies as before
- the anthropic-messages provider builds its httpx clients with `verify=False`
  only when the section asked

Mocks patch `raven.providers.litellm_provider.acompletion` (imported at module
top, so patching `litellm.acompletion` post-import would not be picked up).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from raven.config.schema import ProviderConfig
from raven.contracts.llm_provider import GenerationSettings
from raven.providers.litellm_provider import LiteLLMProvider


class _FakeDelta:
    content: str | None = "hi"


class _FakeChoice:
    delta: _FakeDelta
    finish_reason: str | None = "stop"

    def __init__(self) -> None:
        self.delta = _FakeDelta()


class _FakeChunk:
    choices: list[_FakeChoice]
    usage: Any = None

    def __init__(self) -> None:
        self.choices = [_FakeChoice()]


class _FakeResponse:
    def __init__(self) -> None:
        self.choices = [_FakeChoice()]
        self.usage = None


class _SpyClients:
    """Records every openai.AsyncOpenAI / httpx.AsyncClient built during a call.

    The httpx side is replaced with a subclass rather than a stand-in: the
    OpenAI SDK runs `isinstance(http_client, httpx.AsyncClient)` on what it is
    handed, and only a real subclass keeps that honest.
    """

    openai: list[dict[str, Any]]
    httpx: list[dict[str, Any]]

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import openai

        self.openai = []
        self.httpx = []
        httpx_records = self.httpx
        openai_records = self.openai
        real_httpx = httpx.AsyncClient
        real_openai = openai.AsyncOpenAI

        class SpyHttpx(real_httpx):
            def __init__(self, **kwargs: Any) -> None:
                httpx_records.append(dict(kwargs))
                super().__init__(**kwargs)

        def spy_openai(**kwargs: Any):
            openai_records.append(kwargs)
            return real_openai(**kwargs)

        self._real_httpx = real_httpx
        monkeypatch.setattr(httpx, "AsyncClient", SpyHttpx)
        monkeypatch.setattr(openai, "AsyncOpenAI", spy_openai)

    def client_and_args(self) -> tuple[Any, dict[str, Any]]:
        assert len(self.openai) == 1, self.openai
        return self.openai[0]


def _provider(tls_verify: bool | None, api_base: str = "https://127.0.0.1:8001/v1") -> LiteLLMProvider:
    kwargs: dict[str, Any] = {}
    if tls_verify is not None:
        kwargs["tls_verify"] = tls_verify
    provider = LiteLLMProvider(api_key="test-key", api_base=api_base, default_model="openai/gpt-4o", **kwargs)
    provider.generation = GenerationSettings(timeout=600.0, stream_idle_timeout=180.0, first_byte_timeout=120.0)
    return provider


def test_the_schema_field_round_trips_and_defaults_on() -> None:
    assert ProviderConfig().tls_verify is True
    assert ProviderConfig.model_validate({"tlsVerify": False}).tls_verify is False
    assert ProviderConfig.model_validate({"tls_verify": False}).tls_verify is False


def _spy_acompletion(captured: dict[str, Any], result: Any):
    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return result

    return fake_acompletion


def _stream_of(chunks: list[Any]):
    async def fake_stream():
        for chunk in chunks:
            yield chunk

    return fake_stream()


@pytest.mark.asyncio
async def test_an_opted_out_chat_call_sends_a_prebuilt_client_that_verifies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    spy = _SpyClients(monkeypatch)
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _spy_acompletion(captured, _FakeResponse()))
    await _provider(False).chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")

    args = spy.client_and_args()
    assert args["http_client"] is not None
    assert spy.httpx and spy.httpx[0].get("verify") is False
    # The client reaches the call, and the address still travels the usual kwarg.
    assert captured["client"] is not None
    assert captured["api_base"] == "https://127.0.0.1:8001/v1"


@pytest.mark.asyncio
async def test_an_opted_out_stream_sends_a_prebuilt_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    spy = _SpyClients(monkeypatch)

    async def fake_acompletion(**kwargs: Any):
        captured.update(kwargs)
        return _stream_of([_FakeChunk()])

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    async for _chunk in _provider(False).chat_stream(
        messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o"
    ):
        break

    args = spy.client_and_args()
    assert spy.httpx and spy.httpx[0].get("verify") is False
    assert "client" in captured


@pytest.mark.asyncio
async def test_the_default_verifies_and_builds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    spy = _SpyClients(monkeypatch)
    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _spy_acompletion(captured, _FakeResponse()))
    await _provider(None).chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")

    assert "client" not in captured
    assert spy.openai == [] and spy.httpx == []


@pytest.mark.asyncio
async def test_a_prebuilt_client_is_shared_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    async def fake_acompletion(**kwargs: Any):
        seen.append(kwargs.get("client"))
        return _FakeResponse()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_acompletion)
    provider = _provider(False)
    for _ in range(2):
        await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")

    assert seen[0] is not None and seen[0] is seen[1]


@pytest.mark.asyncio
async def test_the_anthropic_client_verifies_nothing_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AnthropicMessagesProvider(tls_verify=False)` posts through verify-off httpx."""
    from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider

    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    provider = AnthropicMessagesProvider(
        api_key="test-key",
        api_base="https://127.0.0.1:8001",
        default_model="anthropic/claude-sonnet-5",
        tls_verify=False,
    )
    provider.generation = GenerationSettings(timeout=600.0, stream_idle_timeout=180.0, first_byte_timeout=120.0)

    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-5")
    assert built and built[-1].get("verify") is False


@pytest.mark.asyncio
async def test_the_anthropic_client_verifies_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from raven.providers.anthropic_messages_provider import AnthropicMessagesProvider

    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    provider = AnthropicMessagesProvider(
        api_key="test-key",
        api_base="https://127.0.0.1:8001",
        default_model="anthropic/claude-sonnet-5",
    )
    provider.generation = GenerationSettings(timeout=600.0, stream_idle_timeout=180.0, first_byte_timeout=120.0)

    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-5")
    assert built and built[-1].get("verify") is True
