"""Shape tests for OpenAICodexProvider (Responses API), no live call / no key.

Pins that this provider targets the OpenAI Responses endpoint and sends the
experimental Responses beta header — so a switch away from the Responses API
trips a test.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.providers.openai_codex_provider import (
    DEFAULT_CODEX_URL,
    OpenAICodexProvider,
    _build_headers,
    _convert_messages,
    _convert_tool_output,
    _iter_sse,
)


def test_default_url_targets_codex_responses_endpoint():
    assert DEFAULT_CODEX_URL == "https://chatgpt.com/backend-api/codex/responses"
    assert DEFAULT_CODEX_URL.endswith("/codex/responses")


def test_headers_declare_experimental_responses_beta():
    headers = _build_headers(account_id="acct-123", token="tok-abc")
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["Authorization"] == "Bearer tok-abc"
    assert headers["chatgpt-account-id"] == "acct-123"
    assert headers["accept"] == "text/event-stream"


def test_the_model_is_the_callers_to_supply():
    """No built-in default: every id shipped here was refused by the backend, and
    only the account knows which slugs it offers."""
    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol")
    assert provider.get_default_model() == "openai-codex/gpt-5.6-sol"
    # OAuth-based: constructed without an API key.
    assert provider.api_key is None


class _FakeStreamResponse:
    """SSE response stand-in: emits complete events, then stalls forever."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_iter_sse_per_event_idle_timeout_raises():
    """A stream that stalls after a complete event trips the per-event idle cap
    instead of hanging (httpx's per-read timeout would reset on the trickle)."""
    resp = _FakeStreamResponse(['data: {"type": "ping"}', ""])
    events = []
    with pytest.raises(TimeoutError):
        async for event in _iter_sse(resp, timeout=0.05):
            events.append(event)
    assert events == [{"type": "ping"}]


_TINY_PNG_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


def test_convert_tool_output_passes_plain_string_through():
    assert _convert_tool_output("file contents") == "file contents"


def test_convert_tool_output_joins_text_only_blocks_without_json():
    blocks = [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]
    assert _convert_tool_output(blocks) == "line one\nline two"


def test_convert_tool_output_emits_responses_array_when_an_image_is_present():
    blocks = [
        {"type": "text", "text": "screenshot of the dashboard"},
        {"type": "image_url", "image_url": {"url": _TINY_PNG_URI}},
    ]
    assert _convert_tool_output(blocks) == [
        {"type": "input_text", "text": "screenshot of the dashboard"},
        {"type": "input_image", "image_url": _TINY_PNG_URI, "detail": "auto"},
    ]


def test_convert_tool_output_never_serializes_base64_as_prose():
    """Regression: the old fallback json.dumps'd the block list, so the model got
    the image's base64 payload as text instead of a picture -- silently."""
    blocks = [{"type": "image_url", "image_url": {"url": _TINY_PNG_URI}}]
    out = _convert_tool_output(blocks)

    assert isinstance(out, list)
    assert not any("base64" in part.get("text", "") for part in out)
    assert out == [{"type": "input_image", "image_url": _TINY_PNG_URI, "detail": "auto"}]


def test_convert_tool_output_keeps_unknown_blocks_reaching_the_model():
    blocks = [{"type": "citation", "source": "docs"}]
    assert _convert_tool_output(blocks) == '{"type": "citation", "source": "docs"}'


def test_convert_messages_wires_tool_output_into_function_call_output():
    _, items = _convert_messages(
        [
            {
                "role": "tool",
                "tool_call_id": "call_7|fc_7",
                "content": [
                    {"type": "text", "text": "here it is"},
                    {"type": "image_url", "image_url": {"url": _TINY_PNG_URI}},
                ],
            }
        ]
    )

    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_7"
    assert items[0]["output"][1]["type"] == "input_image"


def _capture_body(monkeypatch) -> list[dict]:
    """Run ``chat`` without a network call or a credential, keeping the body."""
    bodies: list[dict] = []

    async def fake_request(url, headers, body, verify, timeout):
        bodies.append(body)

        return "", [], "stop"

    monkeypatch.setattr("raven.providers.openai_codex_provider._request_codex", fake_request)
    monkeypatch.setattr(
        "raven.providers.chatgpt_token.access_token_and_account",
        lambda: ("token", "acct"),
    )

    return bodies


async def test_the_cache_key_is_stable_while_the_conversation_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyed on the transcript, it changed every turn -- so requests sharing a
    cached prefix never landed on the same cache, which is the only thing the key
    is for."""
    bodies = _capture_body(monkeypatch)
    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol")

    await provider.chat([{"role": "system", "content": "you are raven"}, {"role": "user", "content": "one"}])
    await provider.chat(
        [
            {"role": "system", "content": "you are raven"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "two"},
        ]
    )

    assert bodies[0]["prompt_cache_key"] == bodies[1]["prompt_cache_key"]


async def test_a_different_system_prompt_is_a_different_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = _capture_body(monkeypatch)
    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol")

    await provider.chat([{"role": "system", "content": "you are raven"}, {"role": "user", "content": "x"}])
    await provider.chat([{"role": "system", "content": "you are something else"}, {"role": "user", "content": "x"}])

    assert bodies[0]["prompt_cache_key"] != bodies[1]["prompt_cache_key"]


async def test_no_instructions_means_no_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """One key shared by requests that share no prefix is worse than none."""
    bodies = _capture_body(monkeypatch)

    await OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol").chat([{"role": "user", "content": "x"}])

    assert "prompt_cache_key" not in bodies[0]


def test_litellm_still_filters_out_the_cache_key() -> None:
    """The one cost of migrating that a test can see.

    LiteLLM's Responses transformation filters the body through an allow-list,
    and ``prompt_cache_key`` is not on it -- so a migration would keep every test
    green while dropping the field that groups requests for a cache hit.
    """
    import inspect

    from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig

    source = inspect.getsource(ChatGPTResponsesAPIConfig.transform_responses_api_request)

    assert '"prompt_cache_key"' not in source, (
        "LiteLLM now preserves prompt_cache_key: the measured cost of routing codex "
        "through LiteLLMProvider is gone, so re-read this provider's docstring and decide "
        "whether it still has a reason to exist."
    )
