"""The LLM relay: a Raven agent's LiteLLM calls made by the host that launched it."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from raven.contracts.llm_provider import (
    CallRecord,
    ChatDelta,
    ErrorClassification,
    GenerationSettings,
    LLMResponse,
    RunMeta,
    ToolCallRequest,
    TruncationInfo,
)
from raven.providers import call_record, prompt_cache, relay, relay_provider, relay_server, relay_wire, usage_record
from raven.providers.base import LLMProvider
from raven.providers.binding import ModelBinding, active_binding, use_binding
from raven.providers.first_byte import FirstByteTimeoutError, StreamIdleTimeoutError
from raven.providers.litellm_shape import LiteLLMShape


@pytest.fixture(autouse=True)
def _fresh_relay_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(relay.RELAY_ENV, raising=False)
    monkeypatch.delenv(relay.RELAY_TOKEN_ENV, raising=False)
    relay._reset_for_tests()
    yield
    relay._reset_for_tests()


# ---- the wire ----------------------------------------------------------------


def test_the_provider_vocabulary_survives_the_trip() -> None:
    record = CallRecord(http_status=200, served_by="vendor", headers={"x-request-id": "r1"}, body="ok")
    response = LLMResponse(
        content="hi",
        tool_calls=[
            ToolCallRequest(
                id="t1",
                name="read_file",
                arguments={"path": "a.py"},
                run_meta=RunMeta(truncation=TruncationInfo(at_tokens=12), arguments_repaired=True),
            )
        ],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 3},
        thinking_blocks=[{"type": "thinking", "thinking": "x", "signature": "s"}],
        error_classification=ErrorClassification("network", retryable=True),
        truncated=True,
        max_tokens=100,
        reasoning_ms=5,
        call_record=record,
    )
    delta = ChatDelta(content="a", tool_call_delta={"tool_calls": []}, finish_reason="stop", call_record=record)
    generation = GenerationSettings(temperature=0.2, max_tokens=9, reasoning_effort="high")

    for value in (response, delta, generation):
        assert relay_wire.decode(json.loads(json.dumps(relay_wire.encode(value)))) == value


def test_the_not_given_marker_becomes_the_far_sides_own() -> None:
    here, there = object(), object()
    encoded = json.loads(json.dumps(relay_wire.encode({"max_tokens": here, "model": "m"}, sentinel=here)))
    assert relay_wire.decode(encoded, sentinel=there) == {"max_tokens": there, "model": "m"}


def test_a_payload_a_client_library_left_objects_in_still_travels() -> None:
    from pydantic import BaseModel

    class Usage(BaseModel):
        prompt_tokens: int = 4

    assert relay_wire.decode(relay_wire.encode({"usage": Usage(), "n": (1, 2)})) == {
        "usage": {"prompt_tokens": 4},
        "n": [1, 2],
    }


def _litellm_errors() -> list[BaseException]:
    import litellm

    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    rate = httpx.Response(429, request=request, headers={"retry-after": "7"}, text='{"error": "slow down"}')
    errors: list[BaseException] = [
        litellm.RateLimitError("rate limited", llm_provider="openai", model="gpt-4o", response=rate),
        litellm.ContextWindowExceededError("context length exceeded", model="gpt-4o", llm_provider="openai"),
        litellm.BadRequestError(
            "cache_control is not permitted: invalid_request_error", model="claude", llm_provider="anthropic"
        ),
        litellm.APIConnectionError("connection reset", llm_provider="openai", model="gpt-4o"),
        litellm.InternalServerError("upstream exploded", llm_provider="openai", model="gpt-4o"),
    ]
    return errors


def _plain_errors() -> list[BaseException]:
    chained = ValueError("parse failed")
    try:
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as inner:
            raise chained from inner
    except ValueError:
        pass
    return [
        FirstByteTimeoutError(phase="waiting for headers", budget=120.0, waited=120.4),
        StreamIdleTimeoutError(idle=180.0),
        TimeoutError(),
        ConnectionResetError("peer reset"),
        httpx.ReadTimeout("read timed out"),
        chained,
    ]


@pytest.mark.parametrize("make", [_litellm_errors, _plain_errors])
def test_a_relayed_failure_is_judged_as_the_original_would_be(make) -> None:
    for original in make():
        status = LLMProvider._extract_status_code(original)
        rebuilt = relay_wire.decode_exception(
            json.loads(json.dumps(relay_wire.encode_exception(original, status=status)))
        )

        assert LLMProvider.classify_error(rebuilt) == LLMProvider.classify_error(original), original
        assert str(rebuilt) == str(original)
        assert LLMProvider._extract_status_code(rebuilt) == status
        assert isinstance(rebuilt, TimeoutError) == isinstance(original, TimeoutError)
        assert prompt_cache.is_rejection(rebuilt) == prompt_cache.is_rejection(original)
        assert call_record.from_exception(rebuilt, status=status) == call_record.from_exception(original, status=status)


def test_the_two_timeouts_come_back_as_their_own_classes() -> None:
    first = FirstByteTimeoutError(phase="connecting", budget=30.0, waited=30.2)
    idle = StreamIdleTimeoutError(idle=90.0)
    for original in (first, idle):
        rebuilt = relay_wire.decode_exception(relay_wire.encode_exception(original, status=None))
        assert type(rebuilt) is type(original)
        assert str(rebuilt) == str(original)


# ---- the provider and the server ------------------------------------------------------------


class _Wire(LLMProvider):
    """What the host builds in these tests: answers from a script, remembers how it was called."""

    def __init__(self, spec: dict[str, Any], script: dict[str, Any]) -> None:
        super().__init__()
        self.spec = spec
        self.script = script

    def get_default_model(self) -> str:
        return "m"

    def _note_binding(self) -> None:
        bound = active_binding()
        self.script.setdefault("bindings", []).append(None if bound is None else (bound.model, bound.context_window))

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self._note_binding()
        self.script.setdefault("calls", []).append(("chat", kwargs, usage_record._owned.get()))
        return LLMResponse(content=f"answered {kwargs['messages'][-1]['content']}", usage={"prompt_tokens": 1})

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        self._note_binding()
        self.script.setdefault("calls", []).append(("stream", kwargs, usage_record._owned.get()))
        try:
            for piece in self.script.get("pieces", ["a", "b"]):
                yield ChatDelta(content=piece)
                if self.script.get("hang"):
                    await asyncio.sleep(3600)
            if failure := self.script.get("raise"):
                raise failure
            yield ChatDelta(content=None, finish_reason="stop", usage={"completion_tokens": 2})
        except asyncio.CancelledError:
            self.script["cancelled"] = True
            raise


# TCP is the transport a Windows host uses, and the only one a system without
# Unix sockets can serve.
_TRANSPORTS = ["unix", "tcp"] if hasattr(asyncio, "start_unix_server") else ["tcp"]


@pytest.fixture(params=_TRANSPORTS)
async def served(request, monkeypatch: pytest.MonkeyPatch):
    """A relay with a scripted provider, on each transport: TCP is the one Windows hosts use."""
    script: dict[str, Any] = {"specs": []}

    def build(spec: dict[str, Any]) -> LLMProvider:
        script["specs"].append(spec)
        return _Wire(spec, script)

    server = relay_server.RelayServer(build=build, tcp=request.param == "tcp")
    await server.start()
    for key, value in server.env().items():
        monkeypatch.setenv(key, value)
    relay._reset_for_tests()
    yield script, server
    await server.close()


async def test_a_turn_streams_through_the_host(served) -> None:
    script, _ = served
    provider = relay_provider.litellm_provider(default_model="custom/m", provider_name="custom", api_base="http://x/v1")
    assert isinstance(provider, relay_provider.RelayProvider)

    deltas = [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}], model="custom/m")]

    assert [d.content for d in deltas] == ["a", "b", None]
    assert deltas[-1].usage == {"completion_tokens": 2}
    kind, kwargs, owned = script["calls"][0]
    assert kind == "stream" and kwargs["model"] == "custom/m"
    assert kwargs["max_tokens"] is _Wire._SENTINEL
    assert owned, "the host's call must be marked recorded by its caller, the agent"


async def test_a_relayed_call_is_sized_under_the_agents_turn(served) -> None:
    """The output ceiling is the running turn's: the host sees the agent's window, as the agent's own call would.

    ``send_max_tokens`` reads the window from the active binding, so a relayed
    stream sized under the host's context would carry a different number than
    the one the agent's loop reserved.
    """
    script, _ = served
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom", api_base="http://x/v1")
    turn = ModelBinding(provider=provider, model="custom/m", configured_window=32768)

    with use_binding(turn):
        [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}], model="custom/m")]
        await provider.chat(messages=[{"role": "user", "content": "q"}], model="custom/m")
    [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}], model="custom/m")]

    assert script["bindings"] == [("custom/m", 32768), ("custom/m", 32768), None]


async def test_the_host_answers_in_a_context_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relay started inside a host turn: none of that turn reaches an agent's call."""
    script: dict[str, Any] = {"specs": []}
    host_turn = ModelBinding(provider=_Wire({}, script), model="host-model", configured_window=1_000_000)
    with use_binding(host_turn):
        server = relay_server.RelayServer(build=lambda spec: _Wire(spec, script))
        await server.start()
    try:
        for key, value in server.env().items():
            monkeypatch.setenv(key, value)
        relay._reset_for_tests()
        provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
        [d async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}], model="custom/m")]
    finally:
        await server.close()
    assert script["bindings"] == [None]


async def test_a_single_call_answers_through_the_host(served) -> None:
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    response = await provider.chat(messages=[{"role": "user", "content": "q"}])
    assert response.content == "answered q"


async def test_the_host_builds_from_the_construction_as_it_stands_at_the_call(served) -> None:
    """Callers set ``generation`` and ``model_overrides`` after building a provider."""
    script, _ = served
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom", api_key="k")
    provider.generation = GenerationSettings(temperature=0.1, reasoning_effort="low")
    provider.model_overrides = {"m": {"top_p": 0.5}}

    await provider.chat(messages=[{"role": "user", "content": "q"}])

    spec = script["specs"][-1]
    assert spec["api_key"] == "k"
    assert spec["model_overrides"] == {"m": {"top_p": 0.5}}
    assert relay_wire.decode(spec["generation"]) == GenerationSettings(temperature=0.1, reasoning_effort="low")


async def test_a_failure_on_the_host_reaches_the_agent_as_the_same_verdict(served) -> None:
    script, _ = served
    script["raise"] = StreamIdleTimeoutError(idle=5.0)
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")

    with pytest.raises(StreamIdleTimeoutError) as caught:
        async for _ in provider.chat_stream(messages=[{"role": "user", "content": "q"}]):
            pass
    assert provider.classify_error(caught.value).category == LLMProvider.classify_error(script["raise"]).category


async def test_a_stream_the_agent_abandons_is_called_off_on_the_host(served) -> None:
    script, _ = served
    script["hang"] = True
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")

    stream = provider.chat_stream(messages=[{"role": "user", "content": "q"}])
    assert (await stream.__anext__()).content == "a"
    await stream.aclose()

    for _ in range(100):
        if script.get("cancelled"):
            break
        await asyncio.sleep(0.02)
    assert script.get("cancelled")


async def test_a_request_without_the_token_is_refused(served, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "not-the-token")
    relay._reset_for_tests()
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    with pytest.raises(relay.RelayFaultError, match="token"):
        await provider.chat(messages=[{"role": "user", "content": "q"}])


async def _open(server: relay_server.RelayServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    from raven.providers.relay import _split

    kind, where = _split(server.env()[relay.RELAY_ENV])
    if kind == "unix":
        return await asyncio.open_unix_connection(where)
    return await asyncio.open_connection(*where)


async def test_a_peer_without_the_token_is_dropped_before_the_host_reads_a_frame(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On loopback TCP any local account can connect: silence is dropped, and nothing past the token's length is read.

    The second peer announces a frame far larger than it sends; a host that read
    frames before tokens would sit waiting to buffer it.
    """
    _, server = served
    monkeypatch.setattr(relay_server, "_ADMIT_TIMEOUT_S", 0.2)

    reader, writer = await _open(server)
    try:
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    finally:
        writer.close()

    reader, writer = await _open(server)
    try:
        writer.write((256 * 1024 * 1024).to_bytes(4, "big") + b"x" * 4096)
        await writer.drain()
        reply = await asyncio.wait_for(relay_wire.read_frame(reader), timeout=5)
        assert reply is not None and "token" in reply["fault"]
    finally:
        writer.close()


async def test_an_unreachable_host_leaves_the_call_to_this_process(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(relay.RELAY_ENV, f"unix:{tmp_path / 'gone.sock'}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    local = _Wire({}, {})
    monkeypatch.setattr(provider, "_locally", lambda: local)

    response = await provider.chat(messages=[{"role": "user", "content": "q"}])

    assert response.content == "answered q"
    assert not relay.active()


async def test_a_question_that_times_out_leaves_the_next_one_to_the_host(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a host that cannot be reached at all ends the relay for the process."""
    from raven.providers import rates

    def slow_answer(*_args: Any, **_kwargs: Any) -> Any:
        raise relay.RelayUnavailableError("timed out")

    monkeypatch.setattr(relay, "ask", slow_answer)
    local = relay._RELAYED["raven.providers.rates:_may_query_endpoint"]("gpt-4o")

    assert await asyncio.to_thread(rates._may_query_endpoint, "gpt-4o") == local
    assert relay.active()


async def test_a_host_that_cannot_be_reached_ends_the_relay_for_this_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from raven.providers import rates

    monkeypatch.setenv(relay.RELAY_ENV, f"tcp:127.0.0.1:{_closed_port()}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()

    assert rates._may_query_endpoint("gpt-4o") == relay._RELAYED["raven.providers.rates:_may_query_endpoint"]("gpt-4o")
    assert not relay.active()


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_an_unreadable_relay_address_is_refused() -> None:
    with pytest.raises(relay.RelayFaultError):
        relay._split("pipe:/x")


def test_the_offer_is_read_once_and_taken_out_of_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(relay.RELAY_ENV, "unix:/x")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()

    assert relay.target() == ("unix:/x", "t")
    assert relay.RELAY_ENV not in os.environ and relay.RELAY_TOKEN_ENV not in os.environ
    assert relay.active()


def test_without_an_offer_the_direct_provider_is_built() -> None:
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = relay_provider.litellm_provider(default_model="openai/gpt-4o")
    assert type(provider) is LiteLLMProvider
    assert relay_provider.is_litellm_transport(provider)


def test_the_relayed_provider_answers_the_direct_ones_questions_from_the_same_code() -> None:
    from raven.providers.litellm_provider import LiteLLMProvider

    for kwargs in (
        {"default_model": "openrouter/anthropic/claude-sonnet-5", "provider_name": "openrouter", "api_key": "k"},
        {"default_model": "custom/qwen", "provider_name": "custom", "api_base": "http://h/v1/"},
        {"default_model": "anthropic/claude-opus-5", "provider_name": "anthropic"},
    ):
        direct, relayed = LiteLLMProvider(**kwargs), relay_provider.RelayProvider(**kwargs)
        model = kwargs["default_model"]
        assert isinstance(relayed, LiteLLMShape)
        assert relayed.wire_model_id(model) == direct.wire_model_id(model)
        assert relayed.supports_prompt_caching(model) == direct.supports_prompt_caching(model)
        assert relayed.supports_assistant_prefill(model) == direct.supports_assistant_prefill(model)
        assert relayed.emits_unparsed_reasoning() == direct.emits_unparsed_reasoning()
        assert relayed.can_serve(model) == direct.can_serve(model)
        assert relayed.extra_headers == direct.extra_headers
        assert relayed.api_base == direct.api_base
        assert relayed.request_generation(model=model) == direct.request_generation(model=model)


# ---- catalogue questions ---------------------------------------------------------------------


async def test_a_catalogue_question_is_answered_by_the_host_and_remembered(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.providers import rates

    asked: list[str] = []
    real = relay_server.run_relayed

    def counting(name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        asked.append(name)
        return real(name, args, kwargs)

    monkeypatch.setattr(relay_server, "run_relayed", counting)
    expected = relay._RELAYED["raven.providers.rates:_table_entry"]("gpt-4o")

    # From a worker thread: the answer comes from this test's own loop.
    first = await asyncio.to_thread(rates._table_entry, "gpt-4o")
    second = await asyncio.to_thread(rates._table_entry, "gpt-4o")

    assert first == second == expected
    assert asked == ["raven.providers.rates:_table_entry"]


async def test_only_registered_functions_of_listed_modules_run_on_the_host() -> None:
    with pytest.raises(relay.RelayFaultError):
        relay.run_relayed("os:system", ["true"], {})
    with pytest.raises(relay.RelayFaultError):
        relay.run_relayed("raven.providers.rates:resolve_context_window", ["gpt-4o"], {})


def test_the_drivers_directory_is_found_without_importing_litellm() -> None:
    script = textwrap.dedent(
        """
        import sys
        from raven.providers import rates
        found = rates._drivers_dir()
        print(found is not None and found.name == "llms" and found.is_dir(), "litellm" in sys.modules)
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.split() == ["True", "False"]


def test_an_agent_recovers_from_a_cache_refusal_through_a_host_in_another_process() -> None:
    """A downgrade learned in the agent must not be undone by the host that builds its resend.

    Two processes, as in production: each has its own set of suppressed models,
    which an in-process relay would share. The agent's first call is a plain
    chat, the order in which only the host can make the resend carry marks again.
    """
    host = textwrap.dedent(
        """
        import asyncio, json, sys
        import litellm
        from raven.providers import litellm_provider
        from raven.providers.relay_server import RelayServer

        wire = []

        async def refuse_marks(**kwargs):
            wire.append("marked" if "cache_control" in str(kwargs["messages"]) else "unmarked")
            if wire[-1] == "marked":
                raise litellm.BadRequestError(message="400 cache_control is not permitted", model="m", llm_provider="x")
            return litellm.ModelResponse(
                choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

        litellm_provider.acompletion = refuse_marks

        async def main():
            server = RelayServer()
            await server.start()
            print(json.dumps(server.env()), flush=True)
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
            await server.close()
            print(json.dumps(wire), flush=True)

        asyncio.run(main())
        """
    )
    agent = textwrap.dedent(
        """
        import asyncio, json
        from raven.providers import relay
        from raven.providers.relay_provider import RelayProvider

        async def main():
            model = "openrouter/anthropic/claude-3-haiku"
            provider = RelayProvider(default_model=model, provider_name="openrouter", api_key="k")
            messages = [{"role": "system", "content": "You are a careful assistant. " * 200}, {"role": "user", "content": "q"}]
            answer = await provider.chat_with_retry(messages=messages, model=model)
            print(json.dumps({"relayed": relay.active(), "finish": answer.finish_reason, "content": answer.content}))

        asyncio.run(main())
        """
    )
    server = subprocess.Popen([sys.executable, "-c", host], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None and server.stdin is not None
        env = json.loads(server.stdout.readline())
        out = subprocess.run(
            [sys.executable, "-c", agent], capture_output=True, text=True, timeout=180, env={**os.environ, **env}
        )
        server.stdin.close()
        wire = json.loads(server.stdout.readline())
    finally:
        server.kill()
        server.wait(timeout=30)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"relayed": True, "finish": "stop", "content": "ok"}
    assert wire == ["marked", "unmarked"]


def test_a_relayed_process_never_loads_litellm(tmp_path) -> None:
    """The point of the relay, end to end: a turn's call and its catalogue lookups, no LiteLLM import."""
    host = textwrap.dedent(
        """
        import asyncio, json, sys
        from raven.contracts.llm_provider import ChatDelta, LLMResponse
        from raven.providers.base import LLMProvider
        from raven.providers.relay_server import RelayServer

        class Wire(LLMProvider):
            def get_default_model(self): return "m"
            async def chat(self, **kw): return LLMResponse(content="ok")
            async def chat_stream(self, **kw):
                yield ChatDelta(content="ok", finish_reason="stop")

        async def main():
            server = RelayServer(build=lambda spec: Wire())
            await server.start()
            print(json.dumps(server.env()), flush=True)
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
            await server.close()

        asyncio.run(main())
        """
    )
    agent = textwrap.dedent(
        """
        import asyncio, sys
        from raven.providers import rates
        from raven.providers.relay_provider import litellm_provider

        async def main():
            provider = litellm_provider(default_model="custom/m", provider_name="custom", api_base="http://h/v1")
            text = "".join([d.content or "" async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}])])
            window = await asyncio.to_thread(rates.resolve_context_window, "gpt-4o")
            print(type(provider).__name__, text, window is not None, "litellm" in sys.modules, "openai" in sys.modules)

        asyncio.run(main())
        """
    )
    server = subprocess.Popen([sys.executable, "-c", host], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None
        env = json.loads(server.stdout.readline())
        out = subprocess.run(
            [sys.executable, "-c", agent], capture_output=True, text=True, timeout=180, env={**os.environ, **env}
        )
    finally:
        if server.stdin is not None:
            server.stdin.close()
        server.wait(timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["RelayProvider", "ok", "True", "False", "False"]


def test_the_estimators_table_drops_its_unread_copy_and_still_counts() -> None:
    from raven.utils import tokens

    enc = tokens.encoding()
    assert not hasattr(enc, "_mergeable_ranks")
    assert tokens.estimate_message_tokens({"role": "user", "content": "hello world"}) > 0


# ---- edges of the wire, the server and the provider -------------------------------------------


def test_the_default_builder_makes_the_direct_provider_with_the_agents_settings() -> None:
    from raven.providers.litellm_provider import LiteLLMProvider

    generation = GenerationSettings(temperature=0.3)
    spec = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom", api_base="http://h/v1")
    spec.generation = generation
    built = relay_server.build_litellm_provider(spec.spec())
    assert isinstance(built, LiteLLMProvider)
    assert built.generation == generation and built.default_model == "custom/m"


def test_the_spec_carries_every_construction_argument() -> None:
    """The host builds an agent's provider from the spec alone, so whatever the construction takes, the spec carries.

    Listed by hand, the spec misses an argument added to the construction later,
    and the host then builds a provider unlike the agent's without a word.
    ``export_env`` is the host's own choice, not the agent's.
    """
    import inspect

    taken = set(inspect.signature(LiteLLMShape.__init__).parameters) - {"self", "export_env"}
    carried = set(relay_provider.RelayProvider(default_model="custom/m").spec()) - {"generation"}
    assert carried == taken


def test_a_section_without_certificate_checks_keeps_them_off_on_the_host() -> None:
    """``tls_verify`` off (a self-signed gateway) crosses with the construction, to the host and to the fallback."""
    agent = relay_provider.RelayProvider(
        default_model="custom/m", provider_name="custom", api_base="https://gateway.internal/v1", tls_verify=False
    )
    assert relay_server.build_litellm_provider(agent.spec()).tls_verify is False
    assert agent._locally().tls_verify is False
    checked = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    assert relay_server.build_litellm_provider(checked.spec()).tls_verify is True


@pytest.mark.parametrize(
    ("provider_name", "model", "names"),
    [
        ("openrouter", "openrouter/openai/gpt-4o-mini", ("OPENROUTER_API_KEY",)),
        ("zai", "zai/glm-4.6", ("ZAI_API_KEY", "ZHIPUAI_API_KEY")),
    ],
)
def test_building_an_agents_provider_leaves_the_hosts_environment_alone(
    monkeypatch: pytest.MonkeyPatch, provider_name: str, model: str, names: tuple[str, ...]
) -> None:
    """The host's own key stays its own, and nothing it starts inherits the agent's.

    Every call passes the key explicitly, so the provider built for an agent
    needs nothing in the environment; the agent's own process still exports it.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    # The agent's side, which exports the key in its own process as before.
    spec = relay_provider.RelayProvider(default_model=model, provider_name=provider_name, api_key="agent-key").spec()
    monkeypatch.setenv(names[0], "host-key")
    for name in names[1:]:
        monkeypatch.delenv(name, raising=False)
    built = relay_server.build_litellm_provider(spec)

    assert built.api_key == "agent-key"
    assert os.environ[names[0]] == "host-key"
    assert all(name not in os.environ for name in names[1:])
    assert relay_server.RelayServer()._build is relay_server.build_litellm_provider

    monkeypatch.delenv(names[0])
    LiteLLMProvider(default_model=model, provider_name=provider_name, api_key="agent-key")
    assert os.environ[names[0]] == "agent-key"


async def test_frames_that_are_not_frames_are_refused() -> None:
    with pytest.raises(relay_wire.FrameError):
        relay_wire._unpack(b"\xff")
    with pytest.raises(relay_wire.FrameError):
        relay_wire._unpack(b"[1]")
    with pytest.raises(relay_wire.FrameError):
        relay_wire._length((relay_wire.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))

    for data in (b"\x00\x00", b"\x00\x00\x00\x05ab"):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        with pytest.raises(relay_wire.FrameError):
            await relay_wire.read_frame(reader)

    import socket

    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"\x00\x00\x00\x09{}")
        left.shutdown(socket.SHUT_WR)
        with pytest.raises(relay_wire.FrameError):
            relay_wire.recv_frame(right)


def test_a_frame_over_the_bound_is_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_wire, "MAX_FRAME_BYTES", 8)
    with pytest.raises(relay_wire.FrameError):
        relay_wire._pack({"too": "long for eight"})


def test_values_outside_the_vocabulary_are_reduced_not_dropped() -> None:
    import dataclasses

    @dataclasses.dataclass
    class Elsewhere:
        n: int = 1

    class Stubborn:
        def model_dump(self) -> dict:
            raise ValueError("no")

        def __str__(self) -> str:
            return "stubborn"

    assert relay_wire.encode({"a": Elsewhere(), "b": Stubborn(), "c": {1, 1}}) == {
        "a": {"n": 1},
        "b": "stubborn",
        "c": [1],
    }
    record = relay_wire.decode(
        {"__relay__": "CallRecord", "fields": {"http_status": 200, "headers": {"a": 1}, "gone": 1}}
    )
    assert record == CallRecord(http_status=200, headers={"a": "1"})


def test_a_timeout_whose_numbers_do_not_parse_still_comes_back_as_a_timeout() -> None:
    rebuilt = relay_wire.decode_exception(
        {"kind": "first_byte", "timeout": {"budget": "x"}, "message": "slow", "names": []}
    )
    assert isinstance(rebuilt, TimeoutError) and str(rebuilt) == "slow"
    again = relay_wire.decode_exception({"kind": "timeout", "message": "slow", "names": [], "body": "{}"})
    assert type(again) is type(rebuilt) and again.body == "{}"


async def test_the_host_builds_one_provider_per_construction_and_keeps_the_latest(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, server = served
    assert server.serving
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    await provider.chat(messages=[{"role": "user", "content": "q"}])
    await provider.chat(messages=[{"role": "user", "content": "q"}])
    assert len(script["specs"]) == 1

    monkeypatch.setattr(relay_server, "_PROVIDER_CACHE_SIZE", 1)
    other = relay_provider.RelayProvider(default_model="custom/other", provider_name="custom")
    await other.chat(messages=[{"role": "user", "content": "q"}])
    assert len(server._providers) == 1


async def _raw(server: relay_server.RelayServer, frame: dict | None) -> dict | None:
    reader, writer = await _open(server)
    try:
        if frame is None:
            return None
        writer.write(relay_wire.greeting(server.env()[relay.RELAY_TOKEN_ENV]))
        await relay_wire.write_frame(writer, frame)
        return await relay_wire.read_frame(reader)
    finally:
        writer.close()


async def test_requests_the_host_cannot_serve_are_answered_with_a_fault(served) -> None:
    script, server = served
    assert await _raw(server, None) is None
    assert "unknown relay op" in (await _raw(server, {"op": "nope"}))["fault"]
    assert "no provider construction" in (await _raw(server, {"op": "chat", "spec": "x"}))["fault"]
    assert "names no function" in (await _raw(server, {"op": "call", "fn": 3}))["fault"]
    assert "not a relayed" in (await _raw(server, {"op": "call", "fn": "os:system", "args": [], "kwargs": {}}))["fault"]


async def test_a_builder_that_fails_is_a_fault_not_a_dead_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(spec):
        raise ValueError("cannot build")

    server = relay_server.RelayServer(build=broken)
    await server.start()
    try:
        reply = await _raw(server, {"op": "chat", "spec": {}, "args": {}})
        assert "cannot build" in reply["fault"]
        assert server.serving
    finally:
        await server.close()


async def test_a_single_call_that_fails_on_the_host_raises_the_same_class_here(served) -> None:
    script, _ = served

    async def refuse(**kwargs):
        raise TimeoutError()

    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    await provider.chat(messages=[{"role": "user", "content": "q"}])
    built = next(iter(served[1]._providers.values()))
    built.chat = refuse
    with pytest.raises(TimeoutError):
        await provider.chat(messages=[{"role": "user", "content": "q"}])


@pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="the bare host listens on a Unix socket")
async def test_a_stream_the_host_ends_oddly_or_drops_is_a_failure_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host speaking out of turn, played by a bare socket server.

    The socket goes in a short directory of its own, as the relay server puts
    its own: a socket path is capped (104 bytes on macOS), and a test's
    ``tmp_path`` there is already longer.
    """
    replies = iter([{"what": 1}, None])

    async def host(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readline() == relay_wire.greeting("t")
        await relay_wire.read_frame(reader)
        reply = next(replies, None)
        if reply is not None:
            await relay_wire.write_frame(writer, reply)
        writer.close()

    folder = tempfile.mkdtemp(prefix="raven-relay-")
    path = os.path.join(folder, "odd.sock")
    server = await asyncio.start_unix_server(host, path=path)
    monkeypatch.setenv(relay.RELAY_ENV, f"unix:{path}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    try:
        with pytest.raises(relay.RelayFaultError):
            async for _ in provider.chat_stream(messages=[{"role": "user", "content": "q"}]):
                pass
        with pytest.raises(relay_wire.FrameError):
            async for _ in provider.chat_stream(messages=[{"role": "user", "content": "q"}]):
                pass
    finally:
        server.close()
        shutil.rmtree(folder, ignore_errors=True)


async def test_a_stream_without_a_reachable_host_runs_here(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(relay.RELAY_ENV, f"unix:{tmp_path / 'gone.sock'}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    local = _Wire({}, {})
    monkeypatch.setattr(provider, "_locally", lambda: local)

    assert [d.content async for d in provider.chat_stream(messages=[{"role": "user", "content": "q"}])] == [
        "a",
        "b",
        None,
    ]
    assert not relay.active()


def test_the_provider_built_here_follows_this_ones_settings_and_answers_for_the_wire() -> None:
    provider = relay_provider.RelayProvider(
        default_model="openrouter/openai/gpt-4o", provider_name="openrouter", api_key="k"
    )
    provider.model_overrides = {"gpt": {"top_p": 0.1}}
    local = provider._locally()
    assert local.model_overrides == {"gpt": {"top_p": 0.1}}
    assert provider._locally() is local
    provider.disable_auto_cache_control = True
    assert provider._locally().disable_auto_cache_control is True
    model = "openrouter/openai/gpt-4o"
    assert provider.wire_model_id(model) == local.wire_model_id(model)
    assert provider.can_serve(model) == local.can_serve(model)
    assert provider.emits_unparsed_reasoning() == local.emits_unparsed_reasoning()


def test_without_an_offer_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(relay.RELAY_ENV, "unix:/x")
    relay._reset_for_tests()
    assert relay.target() is None
    with pytest.raises(relay.RelayUnavailableError):
        relay.request_frame("call")
    with pytest.raises(relay.RelayUnavailableError):
        relay._connect_sync(1.0)
    with pytest.raises(relay.RelayUnavailableError):
        asyncio.run(relay.connect())
    with pytest.raises(relay_wire.FrameError):
        relay.read_answer(None)


async def test_a_host_that_hangs_up_mid_question_leaves_the_relay_up(served, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang_up(sock):
        raise relay_wire.FrameError("closed")

    monkeypatch.setattr(relay_wire, "recv_frame", hang_up)
    with pytest.raises(relay.RelayUnavailableError) as caught:
        await asyncio.to_thread(relay.ask, "call", fn="x")
    assert not caught.value.connecting


async def test_unhashable_arguments_are_asked_every_time_and_shaped(served, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.providers import rates

    asked: list[Any] = []

    def answer(op: str, **fields: Any) -> Any:
        asked.append(fields)
        return [0.5, 1.5]

    monkeypatch.setattr(relay, "ask", answer)
    monkeypatch.setattr(relay, "_ANSWER_CACHE_SIZE", 1)
    assert await asyncio.to_thread(rates._try_litellm_rates, "m1", 1, 1) == (0.5, 1.5)
    assert await asyncio.to_thread(rates._try_litellm_rates, "m2", 1, 1) == (0.5, 1.5)
    assert await asyncio.to_thread(rates._try_litellm_rates, "m1", 1, 1) == (0.5, 1.5)
    assert len(asked) == 3, "the answer cache holds one entry, so m1 was asked again"

    wrapped = relay.relayed()(lambda value: value)
    assert await asyncio.to_thread(wrapped, ["a list"]) == [0.5, 1.5]


async def test_the_offer_starts_the_relay_once_and_shuts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    warmed: list[int] = []
    monkeypatch.setitem(sys.modules, "litellm", None)
    monkeypatch.delitem(sys.modules, "litellm")
    from raven.providers import litellm_setup

    monkeypatch.setattr(litellm_setup, "warm_up_in_background", lambda: warmed.append(1))
    try:
        first = await relay_server.relay_env()
        second = await relay_server.relay_env()
        assert first == second and first[relay.RELAY_ENV]
        assert warmed == [1]
    finally:
        await relay_server.close_relay()
    assert relay_server._SERVER is None


def test_a_new_event_loop_gets_a_relay_that_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relay left by an ended loop accepts connections nobody answers; the next loop must not hand it out."""
    from raven.providers import litellm_setup

    monkeypatch.setattr(litellm_setup, "warm_up_in_background", lambda: None)

    async def offer() -> dict[str, str]:
        return await relay_server.relay_env()

    first = asyncio.run(offer())

    async def ask_the_second() -> tuple[dict[str, str], Any]:
        env = await relay_server.relay_env()
        monkeypatch.setenv(relay.RELAY_ENV, env[relay.RELAY_ENV])
        monkeypatch.setenv(relay.RELAY_TOKEN_ENV, env[relay.RELAY_TOKEN_ENV])
        relay._reset_for_tests()
        answer = await asyncio.to_thread(
            relay.ask, "call", fn="raven.providers.rates:_table_entry", args=["gpt-4o"], kwargs={}
        )
        await relay_server.close_relay()
        return env, answer

    second, answer = asyncio.run(ask_the_second())
    assert second[relay.RELAY_ENV] != first[relay.RELAY_ENV]
    assert answer == relay._RELAYED["raven.providers.rates:_table_entry"]("gpt-4o")
    if first[relay.RELAY_ENV].startswith("unix:"):
        assert not os.path.exists(first[relay.RELAY_ENV].removeprefix("unix:"))


async def test_a_loop_in_another_thread_is_offered_the_relay_this_loop_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RPC server runs a CLI command's own asyncio.run on a worker thread, beside its own loop."""
    from raven.providers import litellm_setup

    monkeypatch.setattr(litellm_setup, "warm_up_in_background", lambda: None)
    try:
        here = await relay_server.relay_env()
        server = relay_server._SERVER
        beside = await asyncio.to_thread(asyncio.run, relay_server.relay_env())
        assert beside == here
        assert relay_server._SERVER is server and server is not None and server.serving
    finally:
        await relay_server.close_relay()


def test_a_host_without_unix_sockets_serves_on_loopback_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    assert relay_server.RelayServer()._tcp is not hasattr(asyncio, "start_unix_server")
    monkeypatch.delattr(asyncio, "start_unix_server", raising=False)
    assert relay_server.RelayServer()._tcp is True


async def test_a_unix_address_on_a_system_without_unix_sockets_runs_here(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Windows has no Unix-socket API: a ``unix:`` offer there is a relay this process cannot reach."""
    import socket as socket_mod

    from raven.providers import rates

    monkeypatch.setenv(relay.RELAY_ENV, f"unix:{tmp_path / 'relay.sock'}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    monkeypatch.delattr(asyncio, "open_unix_connection", raising=False)
    monkeypatch.delattr(socket_mod, "AF_UNIX", raising=False)
    relay._reset_for_tests()
    local = relay._RELAYED["raven.providers.rates:_may_query_endpoint"]("gpt-4o")

    assert rates._may_query_endpoint("gpt-4o") == local
    assert not relay.active()

    # The first read took the offer out of the environment.
    monkeypatch.setenv(relay.RELAY_ENV, f"unix:{tmp_path / 'relay.sock'}")
    monkeypatch.setenv(relay.RELAY_TOKEN_ENV, "t")
    relay._reset_for_tests()
    assert relay.active()
    provider = relay_provider.RelayProvider(default_model="custom/m", provider_name="custom")
    here = _Wire({}, {})
    monkeypatch.setattr(provider, "_locally", lambda: here)
    assert (await provider.chat(messages=[{"role": "user", "content": "q"}])).content == "answered q"
    assert not relay.active()
