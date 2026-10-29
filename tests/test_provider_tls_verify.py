"""The per-section `tls_verify` knob as the call clients read it.

covers:
- the schema accepts `tls_verify` (camelCase and snake_case) and defaults it on
- LiteLLMProvider(tls_verify=False) hands `acompletion` a prebuilt AsyncOpenAI
  built on an httpx client constructed with `verify=False`, on chat() and
  chat_stream(), one client across calls; the default keeps the `client` kwarg
  absent, so LiteLLM builds and verifies as before
- the anthropic-messages and openai-responses providers build their httpx
  clients with `verify=False` only when the section asked
- end to end, against a gateway signed by a root no store holds: an opted-out
  section reaches it on both of LiteLLM's client families (the OpenAI SDK and
  LiteLLM's own HTTP handler), chat and stream, sending the request once; a
  section that did not opt out is refused
- a failure other than a refused certificate is not sent again

The spy tests patch `raven.providers.litellm_provider.acompletion` (imported at module
top, so patching `litellm.acompletion` post-import would not be picked up).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from raven.config.schema import ProviderConfig
from raven.contracts.llm_provider import GenerationSettings
from raven.providers.litellm_provider import LiteLLMProvider
from tests._tls import OpenAIChat, https_endpoint, keep_loopback_off_proxies, private_ca


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


@pytest.fixture(autouse=True)
def _keep_the_proxy_environment_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every client here is real, and httpx reads the proxy environment as it builds one.

    A `NO_PROXY` entry httpx cannot parse -- a bracketed IPv6 loopback, say --
    raises inside `AsyncClient()`, before any request, so the tests that assert
    on a client's `verify` never reach it. The `gateway` fixture does the same
    for the tests that also make a request.
    """
    keep_loopback_off_proxies(monkeypatch)


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


# LiteLLM sends a request through one of two client families: the OpenAI SDK
# (custom gateways, LM Studio, ...) or its own HTTP handler (vLLM, Ollama,
# DeepSeek, Mistral, ...). Each adopts a prebuilt client of its own kind only,
# so the opt-out is exercised end to end, against a gateway whose certificate
# this machine holds no root for, on one section of each family.
_SECTIONS = [
    pytest.param("custom", "served", id="openai-sdk-route"),
    pytest.param("vllm", "hosted_vllm/served", id="litellm-http-route"),
]


def _section(provider_name: str, model: str, origin: str, *, tls_verify: bool) -> LiteLLMProvider:
    provider = LiteLLMProvider(
        api_key="test-key",
        api_base=f"{origin}/v1",
        default_model=model,
        provider_name=provider_name,
        tls_verify=tls_verify,
    )
    provider.generation = GenerationSettings(timeout=30.0, stream_idle_timeout=30.0, first_byte_timeout=15.0)
    return provider


async def _release(provider: LiteLLMProvider) -> None:
    """Close the HTTP client an opted-out section built, so its session ends with the test's loop."""
    if provider._verify_off_http_handler is not None:
        await provider._verify_off_http_handler.close()


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, type[OpenAIChat]]]:
    """A chat gateway on 127.0.0.1 signed by a root no store holds, and the requests it saw."""
    keep_loopback_off_proxies(monkeypatch)
    # LiteLLMProvider exports a section's key into the process environment.
    for name in ("HOSTED_VLLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "test-key")
    handler = type("Chat", (OpenAIChat,), {"requests": []})
    ca = private_ca(tmp_path / "pki")
    with https_endpoint(ca.server, handler) as origin:
        yield origin, handler


@pytest.mark.parametrize(("provider_name", "model"), _SECTIONS)
async def test_an_opted_out_section_reaches_its_gateway(
    gateway: tuple[str, type[OpenAIChat]], provider_name: str, model: str
) -> None:
    """The request is sent once, whichever client family LiteLLM picks.

    A section on LiteLLM's own HTTP route learns that from a refused handshake,
    which ends before the request is written, so the gateway still sees one.
    """
    origin, handler = gateway
    provider = _section(provider_name, model, origin, tls_verify=False)
    reply = await provider.chat(messages=[{"role": "user", "content": "ping"}], model=model)
    await _release(provider)

    assert (reply.finish_reason, reply.content) == ("stop", "pong")
    assert len(handler.requests) == 1


@pytest.mark.parametrize(("provider_name", "model"), _SECTIONS)
async def test_an_opted_out_section_streams_from_its_gateway(
    gateway: tuple[str, type[OpenAIChat]], provider_name: str, model: str
) -> None:
    origin, handler = gateway
    provider = _section(provider_name, model, origin, tls_verify=False)
    text = "".join(
        [
            delta.content or ""
            async for delta in provider.chat_stream(messages=[{"role": "user", "content": "ping"}], model=model)
        ]
    )
    await _release(provider)

    assert text == "pong"
    assert len(handler.requests) == 1


async def test_the_first_calls_of_an_opted_out_section_reach_its_gateway_together(
    gateway: tuple[str, type[OpenAIChat]],
) -> None:
    """Calls that went out together through the ignored client are each sent again, not only the first."""
    origin, handler = gateway
    provider = _section("vllm", "hosted_vllm/served", origin, tls_verify=False)
    replies = await asyncio.gather(
        *(provider.chat(messages=[{"role": "user", "content": "ping"}], model="hosted_vllm/served") for _ in range(3))
    )
    await _release(provider)

    assert [(reply.finish_reason, reply.content) for reply in replies] == [("stop", "pong")] * 3
    assert len(handler.requests) == 3


@pytest.mark.parametrize(("provider_name", "model"), _SECTIONS)
async def test_a_section_that_did_not_opt_out_still_refuses_the_gateway(
    gateway: tuple[str, type[OpenAIChat]], provider_name: str, model: str
) -> None:
    origin, handler = gateway
    reply = await _section(provider_name, model, origin, tls_verify=True).chat(
        messages=[{"role": "user", "content": "ping"}], model=model
    )

    assert reply.finish_reason == "error"
    assert handler.requests == []


@pytest.mark.parametrize("tls_verify", [False, True])
async def test_the_responses_client_verifies_as_the_section_asked(
    monkeypatch: pytest.MonkeyPatch, tls_verify: bool
) -> None:
    from raven.providers.openai_responses_provider import OpenAIResponsesProvider

    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    provider = OpenAIResponsesProvider(
        api_key="test-key", api_base="https://127.0.0.1:8001/v1", default_model="gpt-5", tls_verify=tls_verify
    )
    provider.generation = GenerationSettings(timeout=600.0, stream_idle_timeout=180.0, first_byte_timeout=120.0)

    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-5")
    assert built and built[-1].get("verify") is tls_verify


@pytest.mark.parametrize("tls_verify", [False, True])
async def test_the_azure_client_verifies_as_the_section_asked(
    monkeypatch: pytest.MonkeyPatch, tls_verify: bool
) -> None:
    """`azure` is another provider the factory builds, and its call client reads the same field."""
    from raven.providers.azure_openai_provider import AzureOpenAIProvider

    built: list[dict[str, Any]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any):
        built.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    provider = AzureOpenAIProvider(
        api_key="test-key", api_base="https://127.0.0.1:8001", default_model="gpt-4o", tls_verify=tls_verify
    )
    provider.generation = GenerationSettings(timeout=600.0, stream_idle_timeout=180.0, first_byte_timeout=120.0)

    await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")
    assert built and built[-1].get("verify") is tls_verify


@pytest.mark.asyncio
async def test_a_client_that_cannot_be_built_becomes_an_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A construction failure is the call's failure, not an exception out of `chat()`."""
    provider = _provider(False)

    def refuse() -> Any:
        raise httpx.InvalidURL("Invalid port: ':1]'")

    monkeypatch.setattr(provider, "_verify_off_client_for_route", refuse)
    reply = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="openai/gpt-4o")

    assert reply.finish_reason == "error"


@pytest.mark.parametrize("streamed", [False, True], ids=["chat", "stream"])
async def test_an_opted_out_call_that_failed_for_another_reason_is_not_sent_again(
    monkeypatch: pytest.MonkeyPatch, streamed: bool
) -> None:
    """Only a refused certificate says the client was ignored before anything was sent.

    Any other failure may have reached the gateway, so sending again could run
    the request twice.
    """
    sent: list[Any] = []

    async def failing_acompletion(**kwargs: Any):
        sent.append(kwargs.get("client"))
        raise httpx.ReadError("connection reset")

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", failing_acompletion)
    provider = _provider(False)
    messages = [{"role": "user", "content": "hi"}]
    if streamed:
        with pytest.raises(httpx.ReadError):
            async for _delta in provider.chat_stream(messages=messages, model="openai/gpt-4o"):
                pass
    else:
        reply = await provider.chat(messages=messages, model="openai/gpt-4o")
        assert reply.finish_reason == "error"

    assert len(sent) == 1
