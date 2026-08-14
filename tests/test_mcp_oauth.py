"""Unit tests for MCP OAuth support (storage, callback correlation, error mapping)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from raven.agent.tools import mcp_oauth
from raven.agent.tools.mcp_oauth import (
    FileTokenStorage,
    OAuthWaitTimeoutError,
    _Flow,
    is_auth_error,
    resolve_callback,
)

pytestmark = pytest.mark.asyncio

REDIRECT = "http://127.0.0.1:18792/oauth/callback"


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_oauth, "_PENDING", {})
    yield


def _tokens(**kw):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(access_token="at-1", token_type="Bearer", refresh_token="rt-1", **kw)


def _client_info(redirects: list[str]):
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(client_id="cid", redirect_uris=redirects)


async def test_token_roundtrip_and_permissions():
    store = FileTokenStorage("srv", REDIRECT)
    assert await store.get_tokens() is None
    await store.set_tokens(_tokens())
    got = await store.get_tokens()
    assert got is not None and got.access_token == "at-1"
    mode = mcp_oauth.credentials_path("srv").stat().st_mode & 0o777
    assert mode == 0o600


async def test_client_info_port_drift_forces_reregistration():
    store = FileTokenStorage("srv", REDIRECT)
    await store.set_client_info(_client_info([REDIRECT]))
    assert (await store.get_client_info()) is not None

    drifted = FileTokenStorage("srv", "http://127.0.0.1:18793/oauth/callback")
    assert (await drifted.get_client_info()) is None
    # Tokens survive the drift — only the registration is invalidated.
    await store.set_tokens(_tokens())
    assert (await drifted.get_tokens()) is not None


async def test_flow_redirect_then_callback_resolution(monkeypatch):
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    events: list[tuple[str, dict]] = []
    flow = _Flow("srv", lambda ev, p: events.append((ev, p)))
    auth_url = "https://as.example/authorize?client_id=cid&state=st-123&code_challenge=x"
    await flow.redirect(auth_url)
    assert opened == [auth_url]
    assert events[0][0] == "oauth.pending"
    assert events[0][1]["server"] == "srv"

    task = asyncio.create_task(flow.callback())
    await asyncio.sleep(0)
    matched, html = resolve_callback({"state": "st-123", "code": "authcode-9"})
    assert matched and "connected" in html
    code, state = await task
    assert (code, state) == ("authcode-9", "st-123")
    assert ("oauth.done", {"server": "srv", "ok": True}) in events


async def test_callback_unknown_state_rejected():
    matched, html = resolve_callback({"state": "nope", "code": "x"})
    assert not matched
    assert "stale" in html


async def test_callback_consumed_once(monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    flow = _Flow("srv", None)
    await flow.redirect("https://as.example/authorize?state=st-1")
    task = asyncio.create_task(flow.callback())
    await asyncio.sleep(0)
    assert resolve_callback({"state": "st-1", "code": "c"})[0] is True
    assert resolve_callback({"state": "st-1", "code": "c"})[0] is False
    await task


async def test_callback_denial_raises_wait_timeout(monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    events: list[tuple[str, dict]] = []
    flow = _Flow("srv", lambda ev, p: events.append((ev, p)))
    await flow.redirect("https://as.example/authorize?state=st-2")
    task = asyncio.create_task(flow.callback())
    await asyncio.sleep(0)
    matched, html = resolve_callback({"state": "st-2", "error": "access_denied"})
    assert matched and "denied" in html
    with pytest.raises(OAuthWaitTimeoutError):
        await task
    assert events[-1][0] == "oauth.done"
    assert events[-1][1]["ok"] is False


async def test_is_auth_error_mapping():
    from mcp.client.auth import OAuthTokenError

    assert is_auth_error(OAuthTokenError("expired"))
    assert is_auth_error(OAuthWaitTimeoutError("slow user"))

    req = httpx.Request("GET", "https://mcp.example/mcp")
    resp = httpx.Response(401, request=req)
    http_err = httpx.HTTPStatusError("401", request=req, response=resp)
    assert is_auth_error(http_err)
    assert is_auth_error(BaseExceptionGroup("g", [RuntimeError("x"), http_err]))

    wrapped = RuntimeError("outer")
    wrapped.__cause__ = OAuthTokenError("inner")
    assert is_auth_error(wrapped)

    assert not is_auth_error(RuntimeError("plain"))
    resp5 = httpx.Response(500, request=req)
    assert not is_auth_error(httpx.HTTPStatusError("500", request=req, response=resp5))
