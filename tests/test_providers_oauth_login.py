"""The console-free device sign-in a page starts: the pair comes back at once,
the poll goes on in the background, and a second start waits for the first."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import httpx
import pytest

from raven.providers import oauth_login
from raven.providers.minimax_oauth import CLIENT_ID, load_token


@pytest.fixture(autouse=True)
def _isolated_starters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_login, "_STARTERS", dict(oauth_login._STARTERS))
    monkeypatch.setattr(oauth_login, "_PENDING", {})


async def test_start_returns_the_pair_and_keeps_polling_until_the_starter_ends() -> None:
    finished = threading.Event()
    released = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        resolve("https://vendor.test/device", "ABCD-1234", 600)
        released.wait(5)
        finished.set()

    oauth_login._STARTERS["fake_vendor"] = fake
    reply = await oauth_login.start("fake_vendor")
    assert reply == {"verification_uri": "https://vendor.test/device", "user_code": "ABCD-1234", "expires_in": 600}
    assert "fake_vendor" in oauth_login.pending()
    with pytest.raises(RuntimeError):
        await oauth_login.start("fake_vendor")
    released.set()
    await asyncio.wait_for(oauth_login._PENDING["fake_vendor"], 5)
    assert finished.is_set()
    assert oauth_login.pending() == {}


async def test_start_raises_what_the_vendor_raised_before_the_code_existed() -> None:
    def fake(_resolve: oauth_login.Resolve) -> None:
        raise ConnectionError("vendor down")

    oauth_login._STARTERS["fake_vendor"] = fake
    with pytest.raises(ConnectionError):
        await oauth_login.start("fake_vendor")
    await asyncio.gather(*oauth_login._PENDING.values(), return_exceptions=True)
    await asyncio.sleep(0)
    assert oauth_login.pending() == {}


async def test_start_refuses_a_provider_without_a_device_flow() -> None:
    with pytest.raises(LookupError):
        await oauth_login.start("deepseek")


def test_minimax_starter_hands_over_the_pair_then_saves_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    from raven.providers import minimax_oauth

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/device/code"):
            assert form["client_id"] == CLIENT_ID
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

    handed: list[tuple[str, str, int]] = []
    real_login = minimax_oauth.login

    def login_with_fake_vendor(region: str, **kw):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return real_login(region, client=client, sleep_fn=lambda _s: None, **kw)

    monkeypatch.setattr(minimax_oauth, "login", login_with_fake_vendor)
    oauth_login._minimax("global")(lambda uri, code, ttl: handed.append((uri, code, ttl)))
    assert handed[0][:2] == ("https://platform.minimax.io/oauth-authorize", "ABCD")
    assert 0 < handed[0][2] <= 60
    assert load_token("global") is not None
