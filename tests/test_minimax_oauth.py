from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

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
                    "verification_uri": "https://account.minimax.io/device",
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
                "verification_uri": "https://account.minimax.io/device",
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
                    "verification_uri": "https://account.minimax.io/device",
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
    assert seen["api_base"] == "https://api.minimax.io/anthropic/v1"
    assert seen["extra_headers"] == {
        "x-api-key": "access",
        "Authorization": "Bearer access",
    }
