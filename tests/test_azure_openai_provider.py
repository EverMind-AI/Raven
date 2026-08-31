"""Timeout behavior for AzureOpenAIProvider (issue #150).

The Azure path uses a raw httpx client with a per-read timeout, which cannot
bound a backend that trickles bytes. A wall-clock cap wraps the awaited POST so
a stalled endpoint yields a structured, retryable error instead of hanging.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.providers.azure_openai_provider import AzureOpenAIProvider
from raven.providers.base import GenerationSettings


class _HangingClient:
    """httpx.AsyncClient stand-in whose POST never returns."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_HangingClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def post(self, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(10)


class _FakeResponse:
    """httpx.Response stand-in carrying just what the non-200 branch reads."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _non_ok_client_cls(status_code: int, text: str) -> type:
    """Build an httpx.AsyncClient stand-in whose POST returns a fixed non-200 response."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            return _FakeResponse(status_code, text)

    return _Client


def _make_provider(timeout: float) -> AzureOpenAIProvider:
    provider = AzureOpenAIProvider(
        api_key="test-key",
        api_base="https://example.openai.azure.com",
        default_model="gpt-4o",
    )
    provider.generation = GenerationSettings(timeout=timeout)
    return provider


@pytest.mark.asyncio
async def test_chat_wall_clock_cap_returns_classified_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raven.providers.azure_openai_provider.httpx.AsyncClient",
        _HangingClient,
    )
    provider = _make_provider(timeout=0.05)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")
    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "network"
    assert resp.error_classification.retryable is True


@pytest.mark.asyncio
async def test_a_rendered_404_body_classifies_as_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status code is real here (``response.status_code``), unlike the
    swallowed-string path ``classify_error`` degrades to elsewhere -- so this
    is classified from it directly, before the response becomes a string.
    """
    monkeypatch.setattr(
        "raven.providers.azure_openai_provider.httpx.AsyncClient",
        _non_ok_client_cls(404, "Resource not found"),
    )
    provider = _make_provider(timeout=5.0)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")
    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "model_unavailable"
    assert resp.error_classification.should_fallback is True
    assert "404" in (resp.content or "")


def test_a_configured_deployment_decides_the_url_path() -> None:
    """The deployment is a connection parameter, not part of the model id.

    It used to be read off the model id, which forced Azure's ids to be spelled
    without the prefix every other provider carries -- a connection detail
    dictating the shape of a stored model id.
    """
    provider = AzureOpenAIProvider(
        api_key="k",
        api_base="https://x.openai.azure.com",
        default_model="azure_openai/gpt-4o",
        deployment="my-prod-deployment",
    )
    url = provider._build_chat_url("azure_openai/gpt-4o")
    assert "/deployments/my-prod-deployment/chat/completions" in url
    assert "azure_openai" not in url


def test_without_a_deployment_the_model_id_still_names_it() -> None:
    """Configs written before the field exists must keep working unchanged."""
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com")
    assert "/deployments/my-deployment/chat/completions" in provider._build_chat_url("my-deployment")


def test_the_api_version_comes_from_config_rather_than_the_client() -> None:
    """A tenant on another version had no way to say so while it was hardcoded."""
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com", api_version="2025-01-01")
    assert provider._build_chat_url("d").endswith("?api-version=2025-01-01")


@pytest.mark.asyncio
async def test_a_non_200_renders_the_canonical_error_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Azure's error content must carry the canonical
    ``Error calling LLM (<category>@<provider>)`` shape: the CLI's
    diagnosis + fix-hint renderer keys off it, and a raw body here renders
    as a fake agent reply with exit 0."""
    from raven.providers.base import parse_llm_error

    monkeypatch.setattr(
        "raven.providers.azure_openai_provider.httpx.AsyncClient",
        _non_ok_client_cls(401, '{"error":{"message":"invalid subscription key","code":"401"}}'),
    )
    provider = _make_provider(timeout=5.0)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")

    assert resp.finish_reason == "error"
    parsed = parse_llm_error(resp.content)
    assert parsed is not None, resp.content
    category, provider_name, detail = parsed
    assert category == "auth"
    assert provider_name == "azure_openai"
    assert "invalid subscription key" in detail
    assert '{"error"' not in (resp.content or "")


@pytest.mark.asyncio
async def test_a_raised_exception_renders_the_canonical_error_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """The swallowed-exception branch must also produce the canonical shape,
    not a raw ``Error calling Azure OpenAI: repr(e)`` string."""
    from raven.providers.base import parse_llm_error

    class _RaisingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_RaisingClient":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset by peer")

    monkeypatch.setattr("raven.providers.azure_openai_provider.httpx.AsyncClient", _RaisingClient)
    provider = _make_provider(timeout=5.0)
    resp = await provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-4o")

    assert resp.finish_reason == "error"
    parsed = parse_llm_error(resp.content)
    assert parsed is not None, resp.content
    category, provider_name, detail = parsed
    assert category == "network"
    assert provider_name == "azure_openai"
    assert "connection reset by peer" in detail


def test_an_unparseable_response_shape_renders_the_canonical_error_content() -> None:
    """The response-parsing branch is an error outlet too and must not leak
    a raw ``Error parsing Azure OpenAI response`` string past the renderer."""
    from raven.providers.base import parse_llm_error

    provider = _make_provider(timeout=5.0)
    resp = provider._parse_response({})

    assert resp.finish_reason == "error"
    parsed = parse_llm_error(resp.content)
    assert parsed is not None, resp.content
    assert parsed[1] == "azure_openai"


def test_arguments_that_needed_repair_are_reported_as_such() -> None:
    """A cut mid-arguments arrives as an unclosed blob, and json_repair closes
    it silently -- so the parsed call looks well-formed to everything after.

    Measured against a truncated `write_file` blob: every cut position that
    loses a field leaves JSON that will not parse, which makes the repair the
    one local, certain signal that a call did not finish arriving. It was
    already being performed here; only the fact of it was dropped, leaving this
    transport unable to refuse a cut-off call the way the others can.
    """
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com")

    def response(arguments: str) -> dict:
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [{"id": "c1", "function": {"name": "write_file", "arguments": arguments}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    whole = provider._parse_response(response('{"path": "a.py", "content": "done"}'))
    assert whole.tool_calls[0].run_meta is None

    cut = provider._parse_response(response('{"path": "a.py", "content": "import ran'))
    assert cut.tool_calls[0].run_meta is not None
    assert cut.tool_calls[0].run_meta.arguments_repaired is True
    # Still repaired: the signal is additional, not a replacement.
    assert cut.tool_calls[0].arguments["content"] == "import ran"


def _gpt_payload(
    deployment: str,
    *,
    tools: bool,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    provider = AzureOpenAIProvider(api_key="k", api_base="https://x.openai.azure.com")
    tool_list = [{"type": "function", "function": {"name": "f", "parameters": {}}}] if tools else None
    return provider._prepare_request_payload(
        deployment,
        [{"role": "user", "content": "hi"}],
        tools=tool_list,
        reasoning_effort=reasoning_effort,
    )


def test_gpt5_with_tools_defaults_to_no_reasoning() -> None:
    """GPT-5.x chat completions 400 on tools without reasoning_effort=none."""
    payload = _gpt_payload("gpt-5.6-sol", tools=True)
    assert payload["reasoning_effort"] == "none"
    assert "temperature" not in payload


def test_gpt5_tools_force_none_over_configured_effort() -> None:
    payload = _gpt_payload("gpt-5.6-sol", tools=True, reasoning_effort="medium")
    assert payload["reasoning_effort"] == "none"


def test_gpt5_without_tools_keeps_configured_effort() -> None:
    payload = _gpt_payload("gpt-5.6-sol", tools=False, reasoning_effort="medium")
    assert payload["reasoning_effort"] == "medium"


def test_gpt5_without_tools_gets_no_injected_effort() -> None:
    payload = _gpt_payload("gpt-5.6-sol", tools=False)
    assert "reasoning_effort" not in payload


def test_other_deployments_are_untouched() -> None:
    payload = _gpt_payload("gpt-4o", tools=True)
    assert "reasoning_effort" not in payload
    assert payload["temperature"] == 0.7
