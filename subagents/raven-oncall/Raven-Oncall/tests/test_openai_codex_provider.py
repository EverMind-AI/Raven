"""Shape tests for OpenAICodexProvider (Responses API), no live call / no key.

Pins that this provider targets the OpenAI Responses endpoint and sends the
experimental Responses beta header — so a switch away from the Responses API
trips a test.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.providers.base import ProviderHTTPError
from raven.providers.openai_codex_provider import (
    DEFAULT_CODEX_URL,
    OpenAICodexProvider,
    _build_headers,
    _consume_sse,
    _convert_messages,
    _convert_tool_output,
    _friendly_error,
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
    only the account knows which slugs it offers. Omitting it has to fail here
    rather than at the first request, where the id would come back rejected."""
    with pytest.raises(TypeError):
        OpenAICodexProvider()  # type: ignore[call-arg]

    provider = OpenAICodexProvider(default_model="openai-codex/gpt-5.6-sol")
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


@pytest.mark.asyncio
async def test_consume_sse_error_event_keeps_the_structured_code_and_message():
    """The code is the retry signal: without it, an overloaded backend looks
    like an unclassifiable error instead of a retryable one."""
    event = {
        "type": "error",
        "code": "server_is_overloaded",
        "message": "Our servers are currently overloaded. Please try again later.",
    }
    resp = _FakeStreamResponse([f"data: {json.dumps(event)}", ""])

    with pytest.raises(RuntimeError) as exc_info:
        await _consume_sse(resp, timeout=1.0)

    assert "server_is_overloaded" in str(exc_info.value)
    assert "Our servers are currently overloaded. Please try again later." in str(exc_info.value)


@pytest.mark.asyncio
async def test_consume_sse_response_failed_event_keeps_the_nested_error():
    """response.failed nests the same error shape under "response" instead of
    at the event's top level."""
    event = {
        "type": "response.failed",
        "response": {
            "status": "failed",
            "error": {
                "code": "server_is_overloaded",
                "message": "Our servers are currently overloaded.",
            },
        },
    }
    resp = _FakeStreamResponse([f"data: {json.dumps(event)}", ""])

    with pytest.raises(RuntimeError) as exc_info:
        await _consume_sse(resp, timeout=1.0)

    assert "server_is_overloaded" in str(exc_info.value)
    assert "Our servers are currently overloaded." in str(exc_info.value)


def test_consume_sse_error_classifies_as_retryable_server_error():
    """Closes the loop: the RuntimeError raised for a codex error event must
    still land classify_error in the retryable "server" bucket, not unknown."""
    event = {
        "type": "error",
        "code": "server_is_overloaded",
        "message": "Our servers are currently overloaded.",
    }
    resp = _FakeStreamResponse([f"data: {json.dumps(event)}", ""])

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(_consume_sse(resp, timeout=1.0))
    classification = OpenAICodexProvider.classify_error(exc_info.value)

    assert classification.category == "server"
    assert classification.retryable is True
    assert classification.should_fallback is True


def test_http_404_classifies_as_model_unavailable_via_the_live_status():
    """The non-200 branch raises ProviderHTTPError so classify_error reads the
    real status instead of guessing from the rendered text -- a plain 404 body
    carrying none of the model-not-found phrases must still bucket correctly."""
    exc = ProviderHTTPError(404, _friendly_error(404, "Resource not found"))

    classification = OpenAICodexProvider.classify_error(exc)

    assert classification.category == "model_unavailable"
    assert classification.should_fallback is True


def test_chat_classifies_a_wire_404_from_the_live_status(monkeypatch):
    """Pins the raise site itself, not just the exception class: a non-200 off
    the wire must reach ``error_classification`` still carrying its status.
    A plain RuntimeError here degrades the same input to ``unknown``."""
    monkeypatch.setattr("raven.providers.chatgpt_token.access_token_and_account", lambda: ("tok", "acct"))

    class _Resp:
        status_code = 404

        async def aread(self):
            return b"Resource not found"

    class _StreamCM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *args):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return _StreamCM()

    monkeypatch.setattr("raven.providers.openai_codex_provider.httpx.AsyncClient", _Client)
    provider = OpenAICodexProvider(default_model="gpt-5")

    resp = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-5"))

    assert resp.finish_reason == "error"
    assert resp.error_classification is not None
    assert resp.error_classification.category == "model_unavailable"
    assert resp.error_classification.should_fallback is True
    assert "404" in (resp.content or "")


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


def test_chat_error_content_renders_the_canonical_shape(monkeypatch):
    """Codex's swallowed error must carry the canonical
    ``Error calling LLM (<category>@<provider>)`` content the CLI renderer
    parses, not a raw ``Error calling Codex`` string that renders as a fake
    agent reply with exit 0."""
    from raven.providers.base import parse_llm_error

    monkeypatch.setattr("raven.providers.chatgpt_token.access_token_and_account", lambda: ("tok", "acct"))

    class _Resp:
        status_code = 401

        async def aread(self):
            return b"Unauthorized"

    class _StreamCM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *args):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return _StreamCM()

    monkeypatch.setattr("raven.providers.openai_codex_provider.httpx.AsyncClient", _Client)
    provider = OpenAICodexProvider(default_model="gpt-5")

    resp = asyncio.run(provider.chat(messages=[{"role": "user", "content": "hi"}], model="gpt-5"))

    assert resp.finish_reason == "error"
    parsed = parse_llm_error(resp.content)
    assert parsed is not None, resp.content
    category, provider_name, _detail = parsed
    assert category == "auth"
    assert provider_name == "openai_codex"


@pytest.mark.asyncio
async def test_arguments_that_needed_repair_are_reported_as_such():
    """The Responses stream hands over the arguments as text, and a cut turn
    ends that text mid-blob. Parsing it into `{"raw": ...}` on failure kept the
    call dispatchable while telling nothing about why it was malformed: the
    model then reads a schema complaint about a field it never sent.

    Repaired and marked instead, which is what the other transports do -- the
    registry refuses a marked call before validation, so the repaired arguments
    are never acted on.
    """

    class _EndingStream:
        """Unlike `_FakeStreamResponse`, ends rather than stalling -- this case
        needs the consumer to return, not to hit its idle timeout."""

        def __init__(self, lines: list[str]) -> None:
            self._lines = lines

        async def aiter_lines(self):
            for line in self._lines:
                yield line

    def stream(arguments: str) -> "_EndingStream":
        done = {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "write_file", "arguments": arguments},
        }
        completed = {"type": "response.completed", "response": {"status": "completed"}}
        return _EndingStream([f"data: {json.dumps(done)}", "", f"data: {json.dumps(completed)}", ""])

    _, whole, _ = await _consume_sse(stream('{"path": "a.py", "content": "done"}'), timeout=1.0)
    assert whole[0].run_meta is None

    _, cut, _ = await _consume_sse(stream('{"path": "a.py", "content": "import ran'), timeout=1.0)
    assert cut[0].run_meta is not None
    assert cut[0].run_meta.arguments_repaired is True
    assert cut[0].arguments["content"] == "import ran", "repaired, not stuffed into a raw blob"
