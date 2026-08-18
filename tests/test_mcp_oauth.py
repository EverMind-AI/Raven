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
    flow = _Flow("srv", lambda ev, p: events.append((ev, p)), interactive=True)
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


async def test_the_callback_endpoint_is_stable_across_gateway_ports(monkeypatch):
    """The redirect URI must not follow the gateway's port.

    It is part of the registration the authorization server keeps, and the
    gateway's port moves (``pick_port`` probes forward when the preferred one
    is taken). A moved redirect makes ``get_client_info`` refuse the stored
    registration, which re-runs dynamic client registration -- and the new
    client cannot use the tokens minted for the old one, so a plugin the user
    already authorized asks to be authorized again.
    """
    monkeypatch.setattr(mcp_oauth, "_callback_base", None)
    monkeypatch.setattr(mcp_oauth, "_fallback_runner", None)

    started: list[int] = []

    class _Site:
        def __init__(self, _runner, _host, port):
            self._port = port

        async def start(self):
            started.append(self._port)

    class _Runner:
        def __init__(self, _app):
            pass

        async def setup(self):
            return None

    monkeypatch.setattr("aiohttp.web.TCPSite", _Site)
    monkeypatch.setattr("aiohttp.web.AppRunner", _Runner)

    uri = await mcp_oauth._ensure_callback_endpoint()

    # The fixed port, not an ephemeral one -- an ephemeral port is a new
    # registration on every process start.
    assert started == [mcp_oauth.CALLBACK_PORT]
    assert uri == f"http://127.0.0.1:{mcp_oauth.CALLBACK_PORT}/oauth/callback"


async def test_a_taken_port_moves_to_the_next_one_and_stays_there(monkeypatch):
    """One fixed port is only stable until something else takes it.

    That is not hypothetical: the first port chosen for this was already held
    by a long-running process on the author's machine, so every launch fell
    back to an ephemeral port and re-registered -- the exact bug the fixed port
    was added to prevent, restored and now silent. The ladder is deterministic,
    so whatever answers today answers tomorrow.
    """
    monkeypatch.setattr(mcp_oauth, "_callback_base", None)
    monkeypatch.setattr(mcp_oauth, "_fallback_runner", None)

    taken = {mcp_oauth.CALLBACK_PORT, mcp_oauth.CALLBACK_PORT + 1}
    started: list[int] = []

    class _Site:
        def __init__(self, _runner, _host, port):
            self._port = port

        async def start(self):
            if self._port in taken:
                raise OSError("address in use")
            started.append(self._port)

    class _Runner:
        def __init__(self, _app):
            pass

        async def setup(self):
            return None

    monkeypatch.setattr("aiohttp.web.TCPSite", _Site)
    monkeypatch.setattr("aiohttp.web.AppRunner", _Runner)

    uri = await mcp_oauth._ensure_callback_endpoint()

    landed = mcp_oauth.CALLBACK_PORT + 2
    assert started == [landed]
    assert uri == f"http://127.0.0.1:{landed}/oauth/callback"
    # Not 0: an ephemeral port would re-register on every start.
    assert 0 not in started


async def test_only_an_exhausted_ladder_falls_back_to_an_ephemeral_port(monkeypatch):
    monkeypatch.setattr(mcp_oauth, "_callback_base", None)
    monkeypatch.setattr(mcp_oauth, "_fallback_runner", None)

    started: list[int] = []

    class _Site:
        def __init__(self, _runner, _host, port):
            self._port = port

        async def start(self):
            if self._port != 0:
                raise OSError("address in use")
            started.append(self._port)
            self._server = type(
                "S", (), {"sockets": [type("K", (), {"getsockname": lambda _s: ("127.0.0.1", 49999)})()]}
            )()

    class _Runner:
        def __init__(self, _app):
            pass

        async def setup(self):
            return None

    monkeypatch.setattr("aiohttp.web.TCPSite", _Site)
    monkeypatch.setattr("aiohttp.web.AppRunner", _Runner)

    uri = await mcp_oauth._ensure_callback_endpoint()

    assert started == [0]
    assert uri == "http://127.0.0.1:49999/oauth/callback"


async def test_background_flow_publishes_the_url_without_opening_a_browser(monkeypatch):
    """A connect nobody asked for must not take the screen.

    The lazy per-turn connect and a config reload both reach this code, so an
    unconditional ``webbrowser.open`` meant typing a message could raise a
    third-party sign-in page. The URL still goes out on ``oauth.pending`` --
    the page offers it, and the reader decides.
    """
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    events: list[tuple[str, dict]] = []
    flow = _Flow("srv", lambda ev, p: events.append((ev, p)))  # interactive defaults to False
    auth_url = "https://as.example/authorize?client_id=cid&state=bg-1&code_challenge=x"
    await flow.redirect(auth_url)

    assert opened == []
    assert events[0][0] == "oauth.pending"
    assert events[0][1]["url"] == auth_url
    assert events[0][1]["interactive"] is False
    # The pending registration still happened, so a callback can still resolve it.
    matched, _ = resolve_callback({"state": "bg-1", "code": "c"})
    assert matched


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


# ── Token expiry survives the process (the silent-refresh anchor) ──


async def test_set_tokens_anchors_an_absolute_expiry():
    import json
    import time

    store = FileTokenStorage("srv", REDIRECT)
    before = time.time()
    await store.set_tokens(_tokens(expires_in=3600))
    after = time.time()

    on_disk = json.loads(mcp_oauth.credentials_path("srv").read_text())
    assert before + 3600 <= on_disk["expires_at"] <= after + 3600
    assert store.stored_token_expiry() == on_disk["expires_at"]


async def test_a_refresh_moves_the_anchor():
    store = FileTokenStorage("srv", REDIRECT)
    await store.set_tokens(_tokens(expires_in=60))
    first = store.stored_token_expiry()
    await store.set_tokens(_tokens(expires_in=3600))
    assert store.stored_token_expiry() > first + 3000


async def test_a_legacy_file_with_a_refresh_token_reads_as_already_expired(monkeypatch):
    """Files written before the anchor existed hold tokens of unknown age.

    Treating them as expired makes the first request a silent refresh instead
    of trusting a stale access token into a 401 -- which is the browser flow.
    Asserted through the SDK's own validity check, not the sentinel value:
    ``is_token_valid`` reads a falsy expiry as "no expiry, still valid", so a
    0.0 sentinel would pass a value-equality assertion while healing nothing.
    """
    import json
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_oauth, "_callback_base", "http://127.0.0.1:18792")
    store = FileTokenStorage("srv", REDIRECT)
    await store.set_tokens(_tokens(expires_in=3600))
    path = mcp_oauth.credentials_path("srv")
    data = json.loads(path.read_text())
    del data["expires_at"]
    path.write_text(json.dumps(data))

    provider = await mcp_oauth.provider_for("srv", SimpleNamespace(url="https://mcp.example/mcp"))
    provider.context.current_tokens = await store.get_tokens()
    assert provider.context.current_tokens.refresh_token, "precondition: the heal targets refreshable tokens"
    assert provider.context.is_token_valid() is False


async def test_tokens_that_declared_no_expiry_seed_nothing():
    from mcp.shared.auth import OAuthToken

    store = FileTokenStorage("srv", REDIRECT)
    await store.set_tokens(OAuthToken(access_token="at-1", token_type="Bearer"))
    assert store.stored_token_expiry() is None


async def test_provider_seeds_the_sdk_expiry_from_disk(monkeypatch):
    """The SDK restores tokens but not their deadline; provider_for supplies it.

    Without the seed, is_token_valid() reads the missing deadline as "valid",
    the refresh branch never runs, and the first 401 re-opens a browser for an
    authorization the stored refresh_token could have renewed silently.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_oauth, "_callback_base", "http://127.0.0.1:18792")
    store = FileTokenStorage("srv", REDIRECT)
    await store.set_tokens(_tokens(expires_in=3600))
    anchored = store.stored_token_expiry()

    provider = await mcp_oauth.provider_for("srv", SimpleNamespace(url="https://mcp.example/mcp"))
    assert provider.context.token_expiry_time == anchored


async def test_provider_seeds_nothing_without_stored_tokens(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_oauth, "_callback_base", "http://127.0.0.1:18792")
    provider = await mcp_oauth.provider_for("srv", SimpleNamespace(url="https://mcp.example/mcp"))
    assert provider.context.token_expiry_time is None


async def test_a_superseded_authorization_link_goes_stale_immediately():
    """A new attempt for the same server dooms the old commit via the epoch, so
    the old link must stop redeeming at the same moment -- a success page for a
    dropped attempt would leave the new one waiting on a click that never comes."""
    flow = _Flow("srv", None)
    await flow.redirect("https://idp.example/authorize?state=old-state")
    assert "srv" in mcp_oauth.auth_wait_servers()
    waiting = asyncio.create_task(flow.callback())
    await asyncio.sleep(0)

    mcp_oauth.cancel_pending("srv")

    assert "srv" not in mcp_oauth.auth_wait_servers()
    with pytest.raises(OAuthWaitTimeoutError, match="superseded"):
        await waiting
    matched, page = resolve_callback({"state": "old-state", "code": "c"})
    assert matched is False
    assert "stale" in page
