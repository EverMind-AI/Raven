"""What one model call leaves behind, and what it must never leave behind.

The record exists because a measured deck run could not be diagnosed from it.
Five calls of about 16.8 MB each came back HTTP 200 with empty content,
`finish_reason: "stop"` and usage all zeros; the stored `llm.output` held the
empty content, the stop reason and the zeros, and nothing else -- no status, no
serving backend, no request id, no body. So "the gateway refused the payload"
and "one backend of the fallback order misbehaved" stayed indistinguishable.

These pin the two halves of that: the fields a record must carry when a response
delivered nothing, and the bounds it must stay inside -- no credential, no image
payload, a capped body and capped headers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from litellm.exceptions import RateLimitError
from litellm.types.utils import ModelResponse, ModelResponseStream

from raven.observability import semconv
from raven.providers import call_record
from raven.providers.base import CallRecord, LLMResponse, ToolCallRequest
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.streaming import stream_llm_call
from raven.utils.images import image_block


def _empty_200(**extra) -> ModelResponse:
    """The shape the incident arrived in: a well-formed 200 that answered nothing."""
    response = ModelResponse(
        id="gen-abc123",
        choices=[{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        model="z-ai/glm-5.3-flash",
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        **extra,
    )
    response._hidden_params = {
        "additional_headers": {
            "x-request-id": "req-9f2c",
            "x-ratelimit-remaining-requests": "42",
            "llm_provider-x-openrouter-trace": "t-1",
        }
    }
    return response


# --------------------------------------------------------------------------- #
# what the record carries when nothing was delivered                          #
# --------------------------------------------------------------------------- #


def test_an_empty_200_records_its_status_backend_and_body() -> None:
    record = call_record.from_response(_empty_200(provider="Z.AI"), usable=False)

    assert record.http_status == 200
    assert record.served_by == "Z.AI"
    assert record.served_model == "z-ai/glm-5.3-flash"
    assert record.response_id == "gen-abc123"
    assert record.headers["x-request-id"] == "req-9f2c"
    assert record.headers["x-ratelimit-remaining-requests"] == "42"
    # The body is what makes the zeros readable rather than merely present.
    assert record.body is not None
    assert '"total_tokens": 0' in record.body


def test_a_gateway_that_names_no_backend_leaves_it_unstated() -> None:
    """None rather than a guess: the model prefix is the gateway raven addressed,
    not the backend that served the call, and reading one as the other would put
    a wrong answer where an absent one belongs."""
    assert call_record.from_response(_empty_200(), usable=False).served_by is None


def test_a_delivered_answer_does_not_get_a_second_copy_of_itself() -> None:
    response = ModelResponse(
        id="gen-1",
        choices=[{"index": 0, "message": {"role": "assistant", "content": "here it is"}, "finish_reason": "stop"}],
        model="m",
    )
    record = call_record.from_response(response, usable=True)

    assert record.body is None
    assert record.http_status == 200


def test_the_provider_attaches_the_record_to_an_empty_stop(monkeypatch) -> None:
    """End of the wire path: what `_parse_response` hands the loop.

    Asked of the provider rather than of the builder, because the record is only
    useful if the one production call site actually fills it in.
    """
    provider = LiteLLMProvider.__new__(LiteLLMProvider)
    monkeypatch.setattr(LiteLLMProvider, "emits_unparsed_reasoning", lambda self: False, raising=False)
    monkeypatch.setattr(LiteLLMProvider, "_normalize_usage", lambda self, usage: dict(usage or {}), raising=False)

    parsed = provider._parse_response(_empty_200(provider="Z.AI"))

    assert parsed.call_record is not None
    assert parsed.call_record.http_status == 200
    assert parsed.call_record.served_by == "Z.AI"
    assert parsed.call_record.body is not None


def test_an_exception_records_the_status_the_category_would_have_swallowed() -> None:
    class _Boom(RuntimeError):
        status_code = 429

    record = call_record.from_exception(_Boom("rate limited, retry in 60s"), status=429)

    assert record.http_status == 429
    assert "rate limited" in (record.body or "")


# --------------------------------------------------------------------------- #
# bounds: no credential, no image payload, capped                            #
# --------------------------------------------------------------------------- #


def test_an_exception_keeps_the_headers_its_httpx_response_carried() -> None:
    """The production failure path hands over ``httpx.Headers``, not a ``dict``.

    Every LiteLLM exception carries an ``httpx.Response``, whose ``headers`` is
    a ``Mapping`` but not a ``dict`` -- so a ``dict`` test dropped the whole set
    on the one path where the request id and the rate-limit block are the
    diagnosis, and left an empty ``headers`` that read as "the gateway sent
    none".
    """
    response = httpx.Response(
        429,
        headers={
            "x-request-id": "req-429",
            "retry-after": "30",
            "x-ratelimit-remaining-tokens": "0",
            "authorization": "Bearer sk-not-a-real-key-000000",
        },
        text='{"error": "rate limited"}',
    )
    record = call_record.from_exception(
        RateLimitError(message="rate limited", llm_provider="openrouter", model="glm", response=response),
        status=429,
    )

    assert record.http_status == 429
    assert record.headers["x-request-id"] == "req-429"
    assert record.headers["retry-after"] == "30"
    assert record.headers["x-ratelimit-remaining-tokens"] == "0"
    assert record.headers["authorization"] == call_record.REPLACEMENT


def test_a_credential_bearing_header_keeps_its_name_and_loses_its_value() -> None:
    """The name is the fact worth keeping; the value is the one that must not be.

    Tested through the library's own prefixing, because that is the shape the
    headers arrive in: a pattern anchored on the bare name would let
    `llm_provider-authorization` walk straight past it.
    """
    kept = call_record.keep_headers(
        {
            "llm_provider-authorization": "Bearer sk-or-v1-DEADBEEFDEADBEEFDEADBEEF",
            "x-api-key": "abcd1234abcd1234",
            "set-cookie": "session=secret-value",
            "proxy-authorization": "Basic Zm9vOmJhcg==",
            "x-request-id": "req-1",
        }
    )

    assert kept["x-request-id"] == "req-1"
    for name in ("llm_provider-authorization", "x-api-key", "set-cookie", "proxy-authorization"):
        assert kept[name] == call_record.REPLACEMENT


def test_an_error_body_quoting_the_request_auth_header_is_scrubbed() -> None:
    """The one realistic way a credential of ours reaches a response body."""
    record = call_record.from_exception(
        RuntimeError('401: {"error":"bad key","sent":{"Authorization":"Bearer sk-or-v1-AAAABBBBCCCCDDDD"}}')
    )

    assert record.body is not None
    assert "sk-or-v1-AAAABBBBCCCCDDDD" not in record.body
    assert call_record.REPLACEMENT in record.body
    # The shape survives, so the reader can still see what the gateway said.
    assert "bad key" in record.body


def test_a_long_body_is_capped_and_says_so() -> None:
    record = call_record.from_exception(RuntimeError("x" * (call_record.MAX_BODY_CHARS * 3)))

    assert record.body is not None
    assert len(record.body) == call_record.MAX_BODY_CHARS
    assert record.body_truncated is True


def test_headers_are_capped_in_count_and_in_value_length() -> None:
    kept = call_record.keep_headers({f"x-h{i}": "v" * 5_000 for i in range(call_record.MAX_HEADERS * 2)})

    assert len(kept) == call_record.MAX_HEADERS + 1  # the kept ones plus the dropped count
    assert kept["x-dropped-header-count"] == str(call_record.MAX_HEADERS)
    assert all(len(v) <= call_record.MAX_HEADER_VALUE_CHARS for k, v in kept.items() if k != "x-dropped-header-count")


def test_an_absent_record_is_absent_rather_than_a_row_of_nulls() -> None:
    assert call_record.as_payload(None) is None
    assert call_record.as_payload(CallRecord()) is None


# --------------------------------------------------------------------------- #
# the request side: counted, never copied                                     #
# --------------------------------------------------------------------------- #


def test_the_request_payload_is_measured_and_pictures_are_only_counted() -> None:
    """Both numbers, because the gap between them is the one that hid the fault.

    A measured request carried 11.24 MB of decoded picture -- under the 12 MB
    image-window budget -- while putting 16.8 MB on the wire, and nothing in the
    record said either number. ``bytes`` measures the messages+tools payload as
    the provider received it, not the body the client went on to serialize, so
    it is pinned against the payload's own compact serialization here.
    """
    payload = "A" * 4_000
    messages = [
        {"role": "user", "content": "describe these"},
        {
            "role": "tool",
            "content": [
                {"type": "text", "text": "page 1"},
                image_block("data:image/png;base64," + payload),
                image_block("data:image/png;base64," + payload),
            ],
        },
    ]
    facts = semconv.request_facts(messages)

    assert facts["images"] == 2
    assert facts["imageBytes"] == 2 * (len(payload) * 3 // 4)
    # The payload holds the pictures base64-encoded, so its size exceeds the
    # decoded count -- and it is exactly the payload, with no pretty-print
    # spacing an HTTP client would never send.
    assert facts["bytes"] > 2 * len(payload)
    assert facts["bytes"] == len(json.dumps({"messages": messages}, ensure_ascii=False, separators=(",", ":")))

    # Counted, never copied: the base64 is not written into the facts.
    assert payload not in json.dumps(facts)


def test_the_output_artifact_carries_the_record_under_one_key() -> None:
    record = call_record.from_response(_empty_200(provider="Z.AI"), usable=False)
    payload = semconv.llm_output_payload(
        LLMResponse(content="", finish_reason="stop", usage={"total_tokens": 0}, call_record=record)
    )

    assert payload["call"]["http_status"] == 200
    assert payload["call"]["served_by"] == "Z.AI"
    assert payload["call"]["response_id"] == "gen-abc123"
    assert payload["call"]["headers"]["x-request-id"] == "req-9f2c"


def test_a_response_from_a_provider_that_reports_nothing_still_serializes() -> None:
    """Every field is optional, and a duck-typed provider fills none of them."""
    payload = semconv.llm_output_payload(LLMResponse(content="hi", tool_calls=[ToolCallRequest("i", "n", {})]))

    assert payload["call"] is None
    json.dumps(payload, default=str)


# --------------------------------------------------------------------------- #
# the streaming path, which is the one the incident took                      #
# --------------------------------------------------------------------------- #


class _FakeWrapper:
    """A stand-in for LiteLLM's ``CustomStreamWrapper``: a stream that opens
    cleanly, retains its chunks, and says nothing at all."""

    def __init__(self) -> None:
        self.custom_llm_provider = "openrouter"
        self.model = "z-ai/glm-5.3-flash"
        self.response_id = "gen-stream-1"
        self.received_finish_reason = "stop"
        self.chunks = [
            {"id": "gen-stream-1", "choices": [{"index": 0, "delta": {"content": None}, "finish_reason": "stop"}]}
        ]
        self._hidden_params = {"additional_headers": {"x-request-id": "req-stream", "llm_provider-authorization": "x"}}
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason="stop")], usage=None
        )


@pytest.mark.asyncio
async def test_an_empty_stream_carries_its_record_through_to_the_response(monkeypatch) -> None:
    """The production wiring on the streaming path, end to end.

    This is the path the incident took: an ACP turn wires a token sink, so the
    loop calls ``chat_stream`` rather than ``chat``. The headers and the serving
    backend live on the library's wrapper, which the delta accumulator never
    sees -- so if the provider stops handing them out on a delta, the record
    goes back to being empty exactly where it matters, silently.
    """

    async def _fake_acompletion(**_kwargs):
        return _FakeWrapper()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(api_key="test", default_model="openrouter/z-ai/glm-5.3-flash")

    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "hi"}], tools=None, model=None)

    assert response.content == ""
    assert response.call_record is not None
    assert response.call_record.http_status == 200
    # The wrapper's own ``custom_llm_provider``/``model`` are the request's
    # adapter and model id, not serving attribution, and this stream's chunk
    # named no backend -- so both fields stay absent rather than echoing the
    # request back as a response fact.
    assert response.call_record.served_by is None
    assert response.call_record.served_model is None
    assert response.call_record.response_id == "gen-stream-1"
    assert response.call_record.headers["x-request-id"] == "req-stream"
    assert response.call_record.headers["llm_provider-authorization"] == call_record.REPLACEMENT
    # A stream has no one body; the chunks it retained stand in for it, and
    # without them an empty stream leaves nothing at all to read.
    assert response.call_record.body is not None
    assert "gen-stream-1" in response.call_record.body


@pytest.mark.asyncio
async def test_a_stream_records_the_backend_the_chunk_named_not_the_one_asked_for(monkeypatch) -> None:
    """Serving attribution on a stream comes from upstream or not at all.

    ``CustomStreamWrapper.custom_llm_provider`` and ``.model`` are its
    constructor arguments, and ``model_response_creator`` pops the upstream
    chunk's own ``model`` before substituting the request's -- so the only
    response-side backend on this path is a ``provider`` member a gateway put in
    the chunk body. Recorded as ``served_by``, ``llm.served_by`` was otherwise a
    second copy of the logical ``llm.provider`` that read as upstream's word.
    """

    class _NamesItsBackend(_FakeWrapper):
        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return ModelResponseStream(
                id="gen-stream-1",
                model="z-ai/glm-5.3-flash",
                provider="Z.AI",
                choices=[{"index": 0, "delta": {"content": None}, "finish_reason": "stop"}],
            )

    async def _fake_acompletion(**_kwargs):
        return _NamesItsBackend()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(api_key="test", default_model="openrouter/z-ai/glm-5.3-flash")

    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "hi"}], tools=None, model=None)

    assert response.call_record is not None
    assert response.call_record.served_by == "Z.AI", "the backend the chunk named"
    assert response.call_record.served_by != "openrouter", "not the request's adapter"
    assert response.call_record.served_model is None, "the library substitutes the request's model on a chunk"


@pytest.mark.asyncio
async def test_a_stream_that_answered_keeps_no_copy_of_its_own_chunks(monkeypatch) -> None:
    class _Answering(_FakeWrapper):
        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="the answer"), finish_reason="stop")],
                usage=None,
            )

    async def _fake_acompletion(**_kwargs):
        return _Answering()

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", _fake_acompletion)
    provider = LiteLLMProvider(api_key="test", default_model="openrouter/z-ai/glm-5.3-flash")

    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "hi"}], tools=None, model=None)

    assert response.content == "the answer"
    assert response.call_record is not None
    assert response.call_record.http_status == 200
    assert response.call_record.body is None
