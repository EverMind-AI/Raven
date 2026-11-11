"""The MiniMax OAuth device flow: token persistence, refresh retries, and rejections."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from raven.contracts.llm_provider import ChatDelta
from raven.providers import usage_record
from raven.providers.minimax_oauth import (
    CLIENT_ID,
    DEVICE_GRANT_TYPE,
    OAUTH_SCOPE,
    MiniMaxOAuthToken,
    _normalize_expiry,
    get_token,
    load_token,
    login,
    save_token,
)
from raven.providers.minimax_oauth_provider import MiniMaxOAuthProvider
from raven.token_wise.registry import StrategyRegistry
from raven.token_wise.usage_tracker import UsageTracker


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    return tmp_path / "minimax_global.json"


def test_login_device_flow_persists_complete_token(token_file: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/device/code"):
            assert form["client_id"] == CLIENT_ID
            assert form["scope"] == OAUTH_SCOPE
            assert form["code_challenge_method"] == "S256"
            assert "response_type" not in form
            return httpx.Response(
                200,
                json={
                    "verification_uri": "https://platform.minimax.io/oauth-authorize",
                    "user_code": "ABCD",
                    "expired_in": int(time.time() * 1000) + 60_000,
                    "interval": 2_000,
                    "state": form["state"],
                },
            )
        assert form["grant_type"] == DEVICE_GRANT_TYPE
        assert form["client_id"] == CLIENT_ID
        assert form["user_code"] == "ABCD"
        if calls == 2:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "access",
                "refresh_token": "refresh",
                "expired_in": int(time.time() * 1000) + 3_600_000,
                "resource_url": "https://api.minimax.io/anthropic/v1",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = login("global", client=client, sleep_fn=lambda _: None, open_browser=False)

    assert calls == 3
    assert token.access == "access"
    assert load_token("global") == token
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_login_rejects_state_mismatch(token_file: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "verification_uri": "https://platform.minimax.io/oauth-authorize",
                "user_code": "ABCD",
                "expired_in": int(time.time() * 1000) + 60_000,
                "interval": 2_000,
                "state": "wrong",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="state mismatch"):
            login("global", client=client, sleep_fn=lambda _: None, open_browser=False)


def test_login_rejects_success_without_refresh_token(token_file: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "verification_uri": "https://platform.minimax.io/oauth-authorize",
                    "user_code": "ABCD",
                    "expired_in": int(time.time() * 1000) + 60_000,
                    "interval": 2_000,
                    "state": form["state"],
                },
            )
        return httpx.Response(200, json={"status": "success", "access_token": "access"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="incomplete credentials"):
            login("global", client=client, sleep_fn=lambda _: None, open_browser=False)
    assert not token_file.exists()


def test_refresh_retries_5xx_and_persists_rotated_token(token_file: Path) -> None:
    save_token(
        "global",
        MiniMaxOAuthToken("old-access", "old-refresh", 0, "https://api.minimax.io/anthropic/v1"),
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "old-refresh"
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expired_in": int(time.time() * 1000) + 3_600_000,
                "resource_url": "https://api.minimax.io/anthropic/v1",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = get_token("global", client=client, sleep_fn=lambda _: None)

    assert calls == 2
    assert token.refresh == "new-refresh"
    assert load_token("global") == token


def test_refresh_does_not_retry_4xx(token_file: Path) -> None:
    save_token(
        "cn",
        MiniMaxOAuthToken("old-access", "old-refresh", 0, "https://api.minimaxi.com/anthropic/v1"),
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="rejected"):
            get_token("cn", client=client, sleep_fn=lambda _: None)
    assert calls == 1


def test_refresh_rejects_expired_result_without_overwriting_token(token_file: Path) -> None:
    original = MiniMaxOAuthToken(
        "old-access",
        "old-refresh",
        0,
        "https://api.minimax.io/anthropic/v1",
    )
    save_token("global", original)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expired_in": 0,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="invalid expiry"):
            get_token("global", client=client, sleep_fn=lambda _: None)

    assert load_token("global") == original


def test_expiry_normalizes_duration_epoch_seconds_and_epoch_milliseconds() -> None:
    now_ms = 2_000_000_000_000
    assert _normalize_expiry(3600, now_ms) == now_ms + 3_600_000
    assert _normalize_expiry(2_100_000_000, now_ms) == 2_100_000_000_000
    assert _normalize_expiry(2_100_000_000_000, now_ms) == 2_100_000_000_000


def test_resource_url_root_falls_back_to_anthropic_v1() -> None:
    from raven.providers.minimax_oauth import _resource_url, oauth_config

    assert _resource_url("https://api.minimax.io", oauth_config("global")) == ("https://api.minimax.io/anthropic/v1")


def test_global_and_cn_tokens_use_distinct_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    global_token = MiniMaxOAuthToken("global-a", "global-r", 4_000_000_000_000, "https://api.minimax.io/anthropic/v1")
    cn_token = MiniMaxOAuthToken("cn-a", "cn-r", 4_000_000_000_000, "https://api.minimaxi.com/anthropic/v1")

    save_token("global", global_token)
    save_token("cn", cn_token)

    assert load_token("global") == global_token
    assert load_token("cn") == cn_token


@pytest.mark.asyncio
async def test_provider_refreshes_and_injects_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    token = MiniMaxOAuthToken(
        "access",
        "refresh",
        int(time.time() * 1000) + 3_600_000,
        "https://api.minimax.io/anthropic/v1",
    )
    seen: dict[str, object] = {}

    monkeypatch.setattr("raven.providers.minimax_oauth_provider.get_token", lambda _: token)

    async def fake_completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr("raven.providers.litellm_provider.acompletion", fake_completion)
    provider = MiniMaxOAuthProvider("global", "minimax-global/MiniMax-M3")
    response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.content == "ok"
    assert seen["model"] == "anthropic/MiniMax-M3"
    assert seen["api_key"] == "access"
    assert seen["api_base"] == "https://api.minimax.io/anthropic"
    assert seen["extra_headers"] == {
        "x-api-key": "access",
        "Authorization": "Bearer access",
    }


@pytest.mark.asyncio
async def test_provider_stream_forwards_every_argument_to_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage seam reads the parent's keyword ``model``; a positional forward
    leaves it reading the tools list instead. Every argument keeps its name."""
    token = MiniMaxOAuthToken(
        "access",
        "refresh",
        int(time.time() * 1000) + 3_600_000,
        "https://api.minimax.io/anthropic/v1",
    )
    monkeypatch.setattr("raven.providers.minimax_oauth_provider.get_token", lambda _: token)
    seen: dict[str, object] = {}

    async def fake_parent_stream(self, *args, **kwargs):
        seen.update(args=args, **kwargs)
        yield ChatDelta(content="ok", finish_reason="stop")

    monkeypatch.setattr("raven.providers.litellm_provider.LiteLLMProvider.chat_stream", fake_parent_stream)
    provider = MiniMaxOAuthProvider("global", "minimax-global/MiniMax-M3")
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    deltas = [
        delta
        async for delta in provider.chat_stream(
            messages=messages,
            tools=tools,
            model="minimax-global/MiniMax-M3",
            max_tokens=123,
            temperature=0.2,
            reasoning_effort="high",
            tool_choice="auto",
        )
    ]

    assert [delta.content for delta in deltas] == ["ok"]
    assert seen == {
        "args": (),
        "messages": messages,
        "tools": tools,
        "model": "minimax-global/MiniMax-M3",
        "max_tokens": 123,
        "temperature": 0.2,
        "reasoning_effort": "high",
        "tool_choice": "auto",
    }


class _StreamDelta:
    def __init__(self, content: str | None = None, tool_calls: Any = None):
        self.content = content
        self.tool_calls = tool_calls


class _StreamChoice:
    def __init__(self, delta: _StreamDelta, finish_reason: str | None = None):
        self.delta = delta
        self.finish_reason = finish_reason


class _StreamChunk:
    def __init__(self, choices: list[_StreamChoice], usage: dict[str, int] | None = None):
        self.choices = choices
        self.usage = usage


class _FakeWireStream:
    """What ``acompletion`` returns on this route: the chunks, nothing else."""

    def __init__(self, chunks: list[_StreamChunk]):
        self._chunks = iter(chunks)

    def __aiter__(self) -> "_FakeWireStream":
        return self

    async def __anext__(self) -> _StreamChunk:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_a_streamed_call_through_the_real_parent_wrapper_is_billed_under_its_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole chain, not a stand-in for either half of it.

    ``MiniMaxOAuthProvider`` forwards to the real ``LiteLLMProvider.chat_stream``,
    and that parent's instrumented wrapper is the one that records. Only the wire
    is faked -- ``acompletion``, the call that would go out on the network -- so
    the recording happens in the code that records in production.

    The provider's own default names a different model than the one requested:
    a lookup that lands on the wrong argument would fall back to it, and the row
    would say so. That is what makes a mis-read distinguishable here rather than
    hidden by a default that happens to repeat the expected name.
    """
    import raven.providers.litellm_provider as litellm_provider

    token = MiniMaxOAuthToken(
        "access",
        "refresh",
        int(time.time() * 1000) + 3_600_000,
        "https://api.minimax.io/anthropic/v1",
    )
    monkeypatch.setattr("raven.providers.minimax_oauth_provider.get_token", lambda _: token)

    async def fake_acompletion(**_kwargs: Any) -> _FakeWireStream:
        return _FakeWireStream(
            [
                _StreamChunk([_StreamChoice(_StreamDelta(content="ok"))]),
                _StreamChunk(
                    [_StreamChoice(_StreamDelta(), finish_reason="stop")],
                    usage={"prompt_tokens": 120, "completion_tokens": 30},
                ),
            ]
        )

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)

    tracker = UsageTracker(telemetry_dir=tmp_path, flush_every=1)
    provider = MiniMaxOAuthProvider("global", "minimax-global/FALLBACK-DEFAULT")
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    with usage_record.bind(StrategyRegistry([tracker])):
        deltas = [
            delta
            async for delta in provider.chat_stream(
                messages=[{"role": "user", "content": "hello"}],
                tools=tools,
                model="minimax-global/MiniMax-M3",
            )
        ]

    assert [delta.content for delta in deltas] == ["ok", None]
    rows = [
        json.loads(line) for line in (tmp_path / f"usage-{date.today().isoformat()}.jsonl").read_text().splitlines()
    ]
    assert [row["model"] for row in rows] == ["minimax-global/MiniMax-M3"]
    assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (120, 30)


@pytest.mark.asyncio
async def test_provider_chat_forwards_every_argument_to_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``chat`` keeps every argument's name, as ``chat_stream`` does.

    ``chat`` and the parent's ``chat`` take the same parameters in the same
    order, so forwarding by position delivers the right values and reads the
    same. The seam that records a call reads the name, though, and a conversion
    back to positional form is how that read went one slot off before.
    """
    from raven.contracts.llm_provider import LLMResponse

    token = MiniMaxOAuthToken(
        "access",
        "refresh",
        int(time.time() * 1000) + 3_600_000,
        "https://api.minimax.io/anthropic/v1",
    )
    monkeypatch.setattr("raven.providers.minimax_oauth_provider.get_token", lambda _: token)
    seen: dict[str, object] = {}

    async def fake_parent_chat(self, *args, **kwargs):
        seen.update(args=args, **kwargs)
        return LLMResponse(content="ok", usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr("raven.providers.litellm_provider.LiteLLMProvider.chat", fake_parent_chat)
    provider = MiniMaxOAuthProvider("global", "minimax-global/MiniMax-M3")
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    response = await provider.chat(
        messages=messages,
        tools=tools,
        model="minimax-global/MiniMax-M3",
        max_tokens=123,
        temperature=0.2,
        reasoning_effort="high",
        tool_choice="auto",
    )

    assert response.content == "ok"
    assert seen == {
        "args": (),
        "messages": messages,
        "tools": tools,
        "model": "minimax-global/MiniMax-M3",
        "max_tokens": 123,
        "temperature": 0.2,
        "reasoning_effort": "high",
        "tool_choice": "auto",
    }
