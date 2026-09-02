"""Unit tests for MCP OAuth support (storage, callback correlation, error mapping)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from mcp.shared.auth import OAuthToken

from raven.mcp import oauth as mcp_oauth
from raven.mcp.oauth import (
    FileTokenStorage,
    OAuthWaitTimeoutError,
    _Flow,
    auth_wait_servers,
    is_auth_error,
    resolve_callback,
)

pytestmark = pytest.mark.asyncio


def _is_seeded(provider) -> bool:
    """Whether the seed subclass is in this provider's ancestry.

    Every provider is wrapped for refresh coordination now, so ``type(...) is
    OAuthClientProvider`` no longer separates a seeded provider from a plain one.
    The seed is what these tests are about.
    """
    return any("Seeded" in cls.__name__ for cls in type(provider).__mro__)


REDIRECT = "http://127.0.0.1:18792/oauth/callback"


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_oauth, "_PENDING", {})
    yield


def _tokens(**kw):
    from mcp.shared.auth import OAuthToken

    return OAuthToken(access_token="at-1", token_type="Bearer", refresh_token="rt-1", **kw)


def _client_info(redirects: list[str], client_id: str = "cid"):
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(client_id=client_id, redirect_uris=redirects)


async def _store_expired(storage, tokens) -> None:
    """Store tokens whose deadline has already passed.

    Tokens with no ``expires_in`` are stored as non-expiring, which is correct
    and which sends every flow down the fast path -- so a test about the refresh
    lock has to age them, or it silently stops testing the lock.
    """
    await storage.set_tokens(tokens)
    path = mcp_oauth.credentials_path(storage._path.stem)  # noqa: SLF001 — test reaches for the file it wrote
    data = json.loads(path.read_text())
    data["expires_at"] = time.time() - 60
    path.write_text(json.dumps(data))


class _FakeProvider:
    """The SDK provider surface the coordination wrapper actually touches.

    Shaped after ``OAuthClientProvider``: ``_initialize`` loads both halves of
    the stored credential, and ``can_refresh_token`` is the SDK's own test --
    tokens with a refresh_token, plus a client to spend it as.
    """

    def __init__(self, **kw):
        self.context = SimpleNamespace(
            storage=kw["storage"],
            current_tokens=None,
            client_info=None,
            token_expiry_time=None,
            client_metadata=SimpleNamespace(redirect_uris=[REDIRECT]),
            update_token_expiry=lambda _t: None,
        )
        # The SDK's own two predicates, copied rather than stubbed: a stub that
        # always says "expired" would hide the whole expiry-adoption question.
        self.context.is_token_valid = lambda: bool(
            self.context.current_tokens
            and self.context.current_tokens.access_token
            and (not self.context.token_expiry_time or time.time() <= self.context.token_expiry_time)
        )
        self.context.can_refresh_token = lambda: bool(
            self.context.current_tokens and self.context.current_tokens.refresh_token and self.context.client_info
        )
        self._initialized = False
        self.sent: list[str] = []

    async def _initialize(self):
        self.context.current_tokens = await self.context.storage.get_tokens()
        self.context.client_info = await self.context.storage.get_client_info()
        self._initialized = True

    async def _handle_refresh_response(self, response):
        return bool(response)

    async def async_auth_flow(self, request):
        tokens = self.context.current_tokens
        self.sent.append(tokens.refresh_token if tokens else "")
        yield request


async def test_token_roundtrip_and_permissions():
    store = FileTokenStorage("srv", REDIRECT)
    assert await store.get_tokens() is None
    await store.set_tokens(_tokens())
    got = await store.get_tokens()
    assert got is not None and got.access_token == "at-1"
    mode = mcp_oauth.credentials_path("srv").stat().st_mode & 0o777
    assert mode == 0o600


async def test_a_drifted_callback_port_does_not_discard_the_registration():
    """The client_id is the half of the credential a refresh runs on.

    Dropping it here is what orphaned the refresh token: the SDK's only answer
    to a missing client is to register a new one, and the token on disk was
    minted for the old client_id. The redirect belongs to the browser flow, and
    a refresh never sends one.
    """
    store = FileTokenStorage("srv", REDIRECT)
    await store.set_client_info(_client_info([REDIRECT]))
    await store.set_tokens(_tokens())

    drifted = FileTokenStorage("srv", "http://127.0.0.1:18793/oauth/callback")
    kept = await drifted.get_client_info()
    assert kept is not None and kept.client_id == "cid"
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
    is taken). A moved redirect costs an authorization-code flow a fresh
    registration, and sends the browser somewhere the user has to sit through
    again -- so the redirect is kept off the gateway's port entirely.
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


async def test_a_connect_that_cannot_wait_does_not_park_on_authorization():
    """``can_park=False`` degrades at once instead of holding its caller.

    A background flow is normally still worth waiting on -- the URL goes out on
    ``oauth.pending``, something relays it, the click lands minutes later. This
    is the other case: a batch pre-flight inside a short-lived command, where
    nobody is standing by. It used to wait the full flow timeout, stalling a
    playbook run 15 minutes per unauthorized server before degrading anyway.
    """
    events: list[tuple[str, dict]] = []
    flow = _Flow("srv", lambda ev, p: events.append((ev, p)), can_park=False)
    await flow.redirect("https://as.example/authorize?client_id=cid&state=nowait-1&code_challenge=x")

    with pytest.raises(OAuthWaitTimeoutError) as excinfo:
        await asyncio.wait_for(flow.callback(), timeout=2)

    assert "cannot wait for it" in str(excinfo.value)
    done = [p for ev, p in events if ev == "oauth.done"]
    assert done and done[-1] == {"server": "srv", "ok": False, "error": "auth_required"}
    # And it claims none of the waiting exemption the handshake watchdog grants.
    assert auth_wait_servers(parkable_only=True) == set()


async def test_a_relayed_background_flow_keeps_its_exemption():
    """The other half of the rule: no browser opened is not no click coming.

    The gateway and the agent's plugin tool both hand the URL to a person and
    return; that flow is not interactive and must still be completable, or
    relaying a link would stop working.
    """
    events: list[tuple[str, dict]] = []
    flow = _Flow("srv-relay", lambda ev, p: events.append((ev, p)))
    await flow.redirect("https://as.example/authorize?client_id=cid&state=relay-1&code_challenge=x")

    assert "srv-relay" in auth_wait_servers(parkable_only=True)
    waiter = asyncio.ensure_future(flow.callback())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=0.2)
    assert resolve_callback({"state": "relay-1", "code": "c"})[0]
    assert await asyncio.wait_for(waiter, timeout=2) == ("c", "relay-1")


async def test_two_providers_do_not_race_a_rotating_refresh_token(monkeypatch, tmp_path):
    """The failure this coordination exists for, end to end.

    The host keeps a provider for its own connection while a bridged dispatch
    needs another for the endpoint's upstream, and both read the same stored
    credential. Where the authorization server rotates refresh tokens, the first
    refresh invalidates what the second still holds in memory: the second gets
    ``invalid_grant`` and falls into browser authorization, which for a dispatch
    means losing the server.

    Asserted on the wire rather than on the object graph: the second provider
    must submit the token that is on disk, not the one it loaded at startup.
    """
    from raven.mcp.oauth import FileTokenStorage, _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("rot", REDIRECT)
    await _store_expired(storage, OAuthToken(access_token="a0", refresh_token="r0", token_type="Bearer"))

    host = cls(storage=storage, server_name="rot", can_park=True)
    bridged = cls(storage=storage, server_name="rot", can_park=False)

    async def run(provider):
        async for _ in provider.async_auth_flow(object()):
            break

    # Both load the same credential, which is the state the race starts from.
    await run(host)
    await run(bridged)
    assert host.sent == ["r0"]
    assert bridged.sent == ["r0"]

    # The host's refresh lands: the stored credential is now a different one, and
    # the one both providers are holding has been retired by the server.
    await _store_expired(storage, OAuthToken(access_token="a1", refresh_token="r1", token_type="Bearer"))
    await run(bridged)

    # Without the re-read this is "r0" again -- the token that no longer works,
    # which is `invalid_grant` and, for a dispatch, a server it has just lost.
    assert bridged.sent == ["r0", "r1"]


async def test_a_contended_refresh_gives_up_rather_than_run_unserialized(monkeypatch, tmp_path):
    """The escape path must not become the race it exists to prevent.

    A caller that cannot park waits a bounded time for the lock. Proceeding past
    that bound was the original bug in miniature: the holder may not have stored
    its replacement yet, so this flow re-reads the same token and consumes it in
    parallel, and one of the two ends up with `invalid_grant` -- a credential
    somebody has to repair. Losing one optional server for this turn is cheaper,
    and the next turn finds the refreshed token on disk.
    """
    from raven.mcp.oauth import FileTokenStorage, OAuthUnavailableError, _coordinated_provider_class

    monkeypatch.setattr(mcp_oauth, "_REFRESH_WAIT", 0.05)

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("contend", REDIRECT)
    await _store_expired(storage, OAuthToken(access_token="a0", refresh_token="r0", token_type="Bearer"))
    bridged = cls(storage=storage, server_name="contend", can_park=False)
    await bridged._initialize()

    # Somebody else is mid-refresh and has not stored a replacement yet.
    lock = mcp_oauth._REFRESH_LOCKS.setdefault("contend", asyncio.Lock())
    await lock.acquire()
    try:
        with pytest.raises(OAuthUnavailableError) as excinfo:
            async for _ in bridged.async_auth_flow(object()):
                break
    finally:
        lock.release()

    assert "re-authorized by another connection" in str(excinfo.value)
    # The point of the assertion: the shared token was never put on the wire.
    assert bridged.sent == []


DRIFTED = "http://127.0.0.1:19999/oauth/callback"


async def _drifted_provider(server: str, *, refresh_token: str | None) -> tuple:
    """A provider whose stored registration was minted for another port."""
    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage(server, REDIRECT)
    await storage.set_client_info(_client_info([DRIFTED], client_id="old-cid"))
    await storage.set_tokens(OAuthToken(access_token="a0", refresh_token=refresh_token, token_type="Bearer"))
    return cls(storage=storage, server_name=server), storage


async def _drive_one_401(provider) -> None:
    """Push one 401 through the wrapper, the way httpx would answer the server."""
    req = httpx.Request("GET", "https://mcp.example/mcp")
    flow = provider.async_auth_flow(req)
    await flow.asend(None)
    with contextlib.suppress(StopAsyncIteration):
        await flow.asend(httpx.Response(401, request=req))
    await flow.aclose()


async def test_a_re_read_never_hands_an_expired_token_a_fresh_lease():
    """The deadline is read back from the anchor, never recomputed.

    ``update_token_expiry`` is ``now + expires_in``, which is only true at the
    moment a token is minted. Applied to one read off disk it gives an hour-old
    access token a fresh hour: the credential reads as valid, the refresh branch
    never runs, and the stale token 401s -- whose handler is a browser, which is
    the failure this coordination exists to prevent.
    """
    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("stale", REDIRECT)
    await storage.set_client_info(_client_info([REDIRECT]))
    await storage.set_tokens(OAuthToken(access_token="a0", refresh_token="r0", token_type="Bearer", expires_in=3600))
    # An hour of wall clock, the way a restart sees it.
    raw = json.loads(mcp_oauth.credentials_path("stale").read_text())
    raw["expires_at"] = time.time() - 60
    mcp_oauth.credentials_path("stale").write_text(json.dumps(raw))

    provider = cls(storage=storage, server_name="stale")
    anchored = storage.stored_token_expiry()

    await provider._adopt_stored_tokens()
    await provider._adopt_stored_tokens()

    assert provider.context.token_expiry_time == anchored
    assert provider.context.is_token_valid() is False


async def test_a_drifted_registration_is_kept_while_a_refresh_can_spend_it():
    """The whole point: the refresh goes out under the stored client_id.

    Re-registering instead is what mints a client the stored refresh_token does
    not belong to -- the authorization server answers `invalid_grant`, and the
    only way back is a human in a browser.
    """
    provider, _ = await _drifted_provider("keep", refresh_token="r0")

    async for _ in provider.async_auth_flow(object()):
        break

    assert provider.context.client_info is not None
    assert provider.context.client_info.client_id == "old-cid"


async def test_a_401_drops_a_drifted_registration_so_the_sdk_re_registers():
    """The 401 is the transition, and the only one that matters.

    Everything before it can still be served by the stored client; from it on,
    the redirect raven listens on now is what goes on the wire, and this client
    is not registered for it. Clearing it is what makes the SDK's
    ``if not client_info`` take the branch that registers.
    """
    provider, _ = await _drifted_provider("drop", refresh_token=None)
    await provider._initialize()
    assert provider.context.client_info is not None

    await _drive_one_401(provider)

    assert provider.context.client_info is None


async def test_a_401_with_a_valid_looking_token_still_drops_the_registration():
    """The case an entry-point check cannot see.

    A token the stored anchor still calls valid can be revoked server-side. No
    refresh is attempted, so a guard keyed on "cannot refresh" never fires -- and
    the authorization then goes out under a client_id registered for a callback
    that moved, which the server answers with ``invalid_redirect_uri``.
    """
    provider, storage = await _drifted_provider("early401", refresh_token="r0")
    raw = json.loads(mcp_oauth.credentials_path("early401").read_text())
    raw["expires_at"] = time.time() + 3600
    mcp_oauth.credentials_path("early401").write_text(json.dumps(raw))
    await provider._initialize()
    provider.context.token_expiry_time = storage.stored_token_expiry()
    assert provider.context.is_token_valid() is True

    await _drive_one_401(provider)

    assert provider.context.client_info is None


async def test_a_registration_that_still_matches_survives_a_401():
    """The drop is keyed on the redirect and nothing else -- otherwise every 401
    on a healthy server throws away a perfectly good registration."""
    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("match", REDIRECT)
    await storage.set_client_info(_client_info([REDIRECT], client_id="live-cid"))
    await storage.set_tokens(OAuthToken(access_token="a0", token_type="Bearer"))
    provider = cls(storage=storage, server_name="match")
    await provider._initialize()

    await _drive_one_401(provider)

    assert provider.context.client_info is not None
    assert provider.context.client_info.client_id == "live-cid"


async def test_a_declared_client_replaces_the_dropped_one_rather_than_a_registration():
    """A catalog-declared client is aimed at the redirect in use, so it can run
    the authorization the dropped one cannot -- and using it saves the round trip
    a dynamic registration would cost."""
    provider, storage = await _drifted_provider("declared", refresh_token=None)
    storage.use_preregistered_client(_client_info([REDIRECT], client_id="pub-client"))
    await provider._initialize()

    await _drive_one_401(provider)

    assert provider.context.client_info is not None
    assert provider.context.client_info.client_id == "pub-client"


async def test_replacing_the_registration_takes_its_tokens_with_it():
    """A token pair belongs to one client_id and dies with it.

    The SDK stores a fresh registration before it opens the browser, so an
    authorization the user abandons would otherwise leave the file holding a
    client nobody authorized and tokens nobody can spend -- and the next connect
    would put that pair on the wire to find out.
    """
    store = FileTokenStorage("replaced", REDIRECT)
    await store.set_client_info(_client_info([REDIRECT], client_id="old-cid"))
    await store.set_tokens(_tokens(expires_in=3600))
    assert (await store.get_tokens()) is not None

    await store.set_client_info(_client_info([REDIRECT], client_id="new-cid"))

    assert (await store.get_tokens()) is None
    assert store.stored_token_expiry() is None
    assert (await store.get_client_info()).client_id == "new-cid"


async def test_rewriting_the_same_registration_keeps_its_tokens():
    """Only a change of client_id retires a credential. A rewrite that lands the
    same client_id must not cost the tokens it still owns."""
    store = FileTokenStorage("same", REDIRECT)
    await store.set_client_info(_client_info([REDIRECT], client_id="cid-1"))
    await store.set_tokens(_tokens(expires_in=3600))

    await store.set_client_info(_client_info([REDIRECT], client_id="cid-1"))

    assert (await store.get_tokens()) is not None


async def test_a_stored_client_outranks_a_declared_one_while_it_can_refresh():
    """The stored refresh token was minted for the stored client_id and no
    other. Handing it to the catalog's client is an `invalid_grant` and a browser
    trip that did not need to happen."""
    storage = FileTokenStorage("outrank", REDIRECT)
    await storage.set_client_info(_client_info([DRIFTED], client_id="old-cid"))
    await storage.set_tokens(OAuthToken(access_token="a0", refresh_token="r0", token_type="Bearer"))
    storage.use_preregistered_client(_client_info([REDIRECT], client_id="pub-client"))

    got = await storage.get_client_info()
    assert got is not None and got.client_id == "old-cid"


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


async def test_a_response_without_expires_in_is_stored_as_non_expiring():
    """ "No deadline stated" and "deadline unknown" are opposite answers.

    A server may legally omit ``expires_in``, and long-lived tokens usually do.
    Recording that as an absent key made it indistinguishable from a file
    written before the anchor existed -- so every flow re-applied the
    unknown-age sentinel, refreshed, got another response without
    ``expires_in``, and went round again. A rotating refresh token is then spent
    once per request.
    """
    from mcp.shared.auth import OAuthToken

    store = FileTokenStorage("noexp", REDIRECT)
    await store.set_tokens(OAuthToken(access_token="a1", refresh_token="r1", token_type="Bearer"))

    stored = json.loads(mcp_oauth.credentials_path("noexp").read_text())
    assert "expires_at" in stored and stored["expires_at"] is None
    assert store.stored_token_expiry() is None


async def test_the_unknown_age_sentinel_retires_itself_after_one_refresh():
    """It exists to make the FIRST request refresh, and storing the result is
    what ends it -- otherwise the heal it triggers re-arms it."""
    store = FileTokenStorage("legacy", REDIRECT)
    path = mcp_oauth.credentials_path("legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {"access_token": "a0", "refresh_token": "r0", "token_type": "Bearer"}}))
    assert store.stored_token_expiry() == 1.0

    from mcp.shared.auth import OAuthToken

    await store.set_tokens(OAuthToken(access_token="a1", refresh_token="r1", token_type="Bearer"))

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


# ---------------------------------------------------------------------------
# catalog-carried authorization-server facts: what stays off the wire, and
# what happens when the facts are wrong
# ---------------------------------------------------------------------------

SERVER_URL = "https://mcp.example/mcp"
PRM_URL = "https://mcp.example/.well-known/oauth-protected-resource/mcp"
ENDPOINTS = {
    "issuer": "https://as.example",
    "authorization_endpoint": "https://as.example/authorize",
    "token_endpoint": "https://as.example/token",
    "registration_endpoint": "https://as.example/register",
    "scopes": ["read"],
    "resource": SERVER_URL,
}


def _server_cfg(url: str = SERVER_URL, **endpoints):
    from raven.config.schema import MCPOAuthConfig, MCPServerConfig

    return MCPServerConfig(url=url, auth="oauth", oauth=MCPOAuthConfig(**endpoints))


def _unauthorized(request):
    return httpx.Response(
        401,
        headers={"WWW-Authenticate": f'Bearer realm="OAuth", resource_metadata="{PRM_URL}"'},
        request=request,
    )


async def _requests_before_the_browser(provider, *, replies=None):
    """Drive one auth flow through the 401 and collect what it puts on the wire.

    ``replies`` answers requests by URL; anything unanswered ends the drive, so
    a test asserts on the request it stopped at.
    """
    replies = replies or {}
    seen: list[str] = []
    first = httpx.Request("POST", SERVER_URL)
    flow = provider.async_auth_flow(first)
    try:
        outgoing = await flow.asend(None)
        seen.append(str(outgoing.url))
        reply = _unauthorized(outgoing)
        while True:
            outgoing = await flow.asend(reply)
            seen.append(str(outgoing.url))
            canned = replies.get(str(outgoing.url))
            if canned is None:
                return seen
            reply = httpx.Response(200, json=canned, request=outgoing)
    finally:
        await flow.aclose()


@pytest.fixture
def _fixed_callback(monkeypatch):
    monkeypatch.delenv(mcp_oauth.NO_SEED_ENV, raising=False)
    monkeypatch.setattr(mcp_oauth, "_callback_base", "http://127.0.0.1:18792")
    yield


async def test_catalog_endpoints_keep_discovery_off_the_wire(_fixed_callback):
    """The two discovery documents come from the config, so the browser opens
    one round trip after the 401 instead of three."""
    provider = await mcp_oauth.provider_for("srv", _server_cfg(**ENDPOINTS))
    seen = await _requests_before_the_browser(provider)

    assert not any("/.well-known/" in url for url in seen), seen
    assert seen[-1] == "https://as.example/register"
    assert str(provider.context.oauth_metadata.token_endpoint) == "https://as.example/token"
    assert provider.context.protected_resource_metadata is not None
    assert provider.context.client_metadata.scope == "read"


async def test_a_server_without_catalog_endpoints_discovers_exactly_as_before(_fixed_callback):

    provider = await mcp_oauth.provider_for("srv", _server_cfg())
    assert not _is_seeded(provider)

    seen = await _requests_before_the_browser(provider)
    assert seen[-1] == PRM_URL


async def test_the_kill_switch_puts_discovery_back_on_the_wire(monkeypatch, _fixed_callback):
    monkeypatch.setenv(mcp_oauth.NO_SEED_ENV, "1")
    provider = await mcp_oauth.provider_for("srv", _server_cfg(**ENDPOINTS))
    seen = await _requests_before_the_browser(provider)
    assert seen[-1] == PRM_URL


async def test_endpoints_the_server_refuses_are_distrusted_next_time(_fixed_callback):
    """A wrong entry costs one round trip, not a broken authorization.

    The registration the seeded metadata pointed at 404s; the next connect for
    the same server discovers instead of trusting the same facts again.
    """
    cfg = _server_cfg(**ENDPOINTS)
    provider = await mcp_oauth.provider_for("srv", cfg)
    first = httpx.Request("POST", SERVER_URL)
    flow = provider.async_auth_flow(first)
    outgoing = await flow.asend(None)
    registration = await flow.asend(_unauthorized(outgoing))
    assert str(registration.url) == "https://as.example/register"
    with pytest.raises(Exception, match="[Rr]egistration"):
        await flow.asend(httpx.Response(404, request=registration))
    await flow.aclose()

    retry = await mcp_oauth.provider_for("srv", cfg)
    assert not _is_seeded(retry)
    assert (await _requests_before_the_browser(retry))[-1] == PRM_URL


async def test_corrected_endpoints_are_believed_again(_fixed_callback):
    """The distrust is keyed to the facts that failed, not to the server."""
    cfg = _server_cfg(**ENDPOINTS)
    storage = FileTokenStorage("srv", REDIRECT)
    storage.disarm_seed(mcp_oauth._fingerprint(cfg.oauth))
    assert not _is_seeded(await mcp_oauth.provider_for("srv", cfg))

    fixed = _server_cfg(**{**ENDPOINTS, "registration_endpoint": "https://as.example/v2/register"})
    seen = await _requests_before_the_browser(await mcp_oauth.provider_for("srv", fixed))
    assert seen[-1] == "https://as.example/v2/register"


async def _answer_the_silent_refresh(cfg, reply: httpx.Response | None = None, **response_kw):
    """Drive a connect whose first request is the SDK's silent refresh.

    The seeded ``token_endpoint`` here is the one the SDK computes on its own
    when ``oauth_metadata`` is still None, which is what the shipped entries
    look like on a cold start -- so the refusal lands on a watched URL.
    """
    storage = FileTokenStorage("srv", REDIRECT)
    await storage.set_tokens(_tokens(expires_in=-1))
    provider = await mcp_oauth.provider_for("srv", cfg)
    flow = provider.async_auth_flow(httpx.Request("POST", SERVER_URL))
    try:
        refresh = await flow.asend(None)
        assert str(refresh.url) == cfg.oauth.token_endpoint, str(refresh.url)
        await flow.asend(reply or httpx.Response(request=refresh, **response_kw))
    finally:
        await flow.aclose()


REFRESHABLE = {**ENDPOINTS, "token_endpoint": "https://mcp.example/token", "client_id": "pub-client"}


async def test_a_dead_refresh_token_does_not_disarm_the_seed(_fixed_callback):
    """`400 invalid_grant` is the most routine event in a token's life.

    It says the stored refresh_token is finished, not that the catalog entry is
    wrong; disarming on it would retire the optimization on every server whose
    tokens ever expire out from under raven.
    """
    cfg = _server_cfg(**REFRESHABLE, redirect_uri=REDIRECT)
    await _answer_the_silent_refresh(cfg, status_code=400, json={"error": "invalid_grant"})

    assert not FileTokenStorage("srv", REDIRECT).seed_disarmed(mcp_oauth._fingerprint(cfg.oauth))
    again = await mcp_oauth.provider_for("srv", cfg)
    assert _is_seeded(again)


async def test_a_token_endpoint_that_is_not_there_still_disarms_the_seed(_fixed_callback):
    """The other side of the boundary: 404 is the entry naming a URL that is not
    a token endpoint, which no re-authorization can fix."""
    cfg = _server_cfg(**REFRESHABLE, redirect_uri=REDIRECT)
    await _answer_the_silent_refresh(cfg, status_code=404)

    assert FileTokenStorage("srv", REDIRECT).seed_disarmed(mcp_oauth._fingerprint(cfg.oauth))

    assert not _is_seeded(await mcp_oauth.provider_for("srv", cfg))


async def _completed(code: str, state: list[str]):
    return code, state[0]


async def test_a_preregistered_client_skips_registration(_fixed_callback):
    provider = await mcp_oauth.provider_for(
        "srv", _server_cfg(**ENDPOINTS, client_id="pub-client", redirect_uri=REDIRECT)
    )
    state: list[str] = []

    async def _redirect(url: str) -> None:
        from urllib.parse import parse_qs, urlparse

        assert url.startswith("https://as.example/authorize?")
        state.append(parse_qs(urlparse(url).query)["state"][0])

    provider.context.redirect_handler = _redirect
    provider.context.callback_handler = lambda: _completed("code-1", state)

    seen = await _requests_before_the_browser(provider)
    assert "https://as.example/register" not in seen
    assert seen[-1] == "https://as.example/token"
    assert provider.context.client_info.client_id == "pub-client"


async def test_a_preregistered_client_is_dropped_when_the_callback_port_moved(_fixed_callback):
    """Its redirect is fixed at the service; ours walked the port ladder. Using
    it would send the browser to a port nobody is listening on."""
    provider = await mcp_oauth.provider_for(
        "srv",
        _server_cfg(**ENDPOINTS, client_id="pub-client", redirect_uri="http://127.0.0.1:19999/oauth/callback"),
    )
    seen = await _requests_before_the_browser(provider)
    assert seen[-1] == "https://as.example/register"


async def test_a_live_peer_adopts_the_client_along_with_the_tokens():
    """Tokens and the client that minted them are one credential.

    The host keeps a provider for its own connection while a bridged dispatch
    needs another, and only the one performing a handoff learns the new owner.
    A peer that adopts the replacement tokens while holding the client they
    replaced sends `invalid_grant` and buys a browser for a credential that
    works.
    """
    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("peer", REDIRECT)
    await storage.set_client_info(_client_info([DRIFTED], client_id="old-cid"))
    await storage.set_tokens(OAuthToken(access_token="a0", refresh_token="r0", token_type="Bearer"))
    storage.use_preregistered_client(_client_info([REDIRECT], client_id="pub-client"))

    peer = cls(storage=storage, server_name="peer")
    await peer._initialize()
    assert peer.context.client_info.client_id == "old-cid"

    # What the other provider's handoff leaves behind.
    storage.forget_client_info()
    await storage.set_tokens(OAuthToken(access_token="a1", refresh_token="r1", token_type="Bearer"))

    await peer._adopt_stored_tokens()

    assert peer.context.current_tokens.refresh_token == "r1"
    assert peer.context.client_info.client_id == "pub-client"


async def test_the_endpoints_a_server_stated_are_written_and_read_back():
    """The note the next process refreshes from.

    Kept here rather than only in tests/integration: that directory is outside
    `norecursedirs`, so CI never collects it, and a write path with no test CI
    runs is a write path nobody is watching.
    """
    store = FileTokenStorage("endpoints", REDIRECT)
    assert store.remembered_metadata() is None

    asm = {
        "issuer": "https://as.example",
        "authorization_endpoint": "https://as.example/oauth/authorize",
        "token_endpoint": "https://as.example/oauth/token",
    }
    store.remember_metadata(asm)

    assert store.remembered_metadata() == asm
    # Idempotent: the same document must not rewrite the file on every response.
    before = mcp_oauth.credentials_path("endpoints").stat().st_mtime_ns
    store.remember_metadata(asm)
    assert mcp_oauth.credentials_path("endpoints").stat().st_mtime_ns == before


async def test_a_note_without_a_token_endpoint_is_no_note_at_all():
    """Half a document would seed the SDK with something it cannot refresh
    against, and the guessed address is the better fallback."""
    store = FileTokenStorage("halfnote", REDIRECT)
    store.remember_metadata({"issuer": "https://as.example"})
    assert store.remembered_metadata() is None


async def test_a_provider_records_what_discovery_taught_it():
    """The hook that turns a discovered document into the next process's note."""
    from types import SimpleNamespace

    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("learned", REDIRECT)
    provider = cls(storage=storage, server_name="learned")
    provider.context.oauth_metadata = SimpleNamespace(
        model_dump=lambda **_: {
            "issuer": "https://as.example",
            "authorization_endpoint": "https://as.example/oauth/authorize",
            "token_endpoint": "https://as.example/oauth/token",
        }
    )

    provider._remember_metadata()

    assert storage.remembered_metadata()["token_endpoint"].endswith("/oauth/token")


async def test_a_provider_records_nothing_when_the_catalog_supplied_it():
    """A declared document is the catalog's answer, not the server's. Recording
    it would outlive the disarm that exists for a declaration gone wrong."""
    from types import SimpleNamespace

    from raven.mcp.oauth import _coordinated_provider_class

    cls = _coordinated_provider_class(_FakeProvider)
    storage = FileTokenStorage("declared", REDIRECT)
    provider = cls(storage=storage, server_name="declared")
    provider._asm_from_catalog = True
    provider.context.oauth_metadata = SimpleNamespace(
        model_dump=lambda **_: {"token_endpoint": "https://as.example/oauth/token"}
    )

    provider._remember_metadata()

    assert storage.remembered_metadata() is None


async def test_a_credential_is_read_as_one_thing():
    """One read, so a peer writing between two reads cannot hand back the tokens
    from after it beside the client from before it."""
    store = FileTokenStorage("unit", REDIRECT)
    await store.set_client_info(_client_info([REDIRECT], client_id="cid-1"))
    await store.set_tokens(_tokens(expires_in=3600))

    tokens, expiry, client = await store.get_credential()

    assert tokens.refresh_token == "rt-1"
    assert expiry == store.stored_token_expiry()
    assert client.client_id == "cid-1"


async def test_the_handoff_to_a_declared_client_takes_the_superseded_one_off_disk():
    """A declared client taking over skips registration, so nothing else will
    overwrite the registration it replaced. Left there, the next process pairs
    the declared client's tokens with a client_id that did not mint them.

    The declared client is not written in its place: it is a catalog fact, and a
    copy here would outlive the entry that justified it.
    """
    store = FileTokenStorage("handoff", REDIRECT)
    await store.set_client_info(_client_info([DRIFTED], client_id="old-cid"))
    store.use_preregistered_client(_client_info([REDIRECT], client_id="pub-client"))

    store.forget_client_info()

    assert json.loads(mcp_oauth.credentials_path("handoff").read_text()).get("client_info") is None
    # With nothing stored, the declared client is what the next read resolves to.
    assert (await store.get_client_info()).client_id == "pub-client"


async def test_a_resource_the_url_does_not_canonicalize_to_is_fetched_instead(_fixed_callback):
    """The audience a token is minted for is never taken on trust: a declared
    resource that would move it is refused, and that document goes on the wire."""
    provider = await mcp_oauth.provider_for("srv", _server_cfg(url="https://mcp.example/sse", **ENDPOINTS))
    prm_url = "https://mcp.example/.well-known/oauth-protected-resource/sse"
    seen = await _requests_before_the_browser(
        provider,
        replies={
            PRM_URL: {"resource": "https://mcp.example", "authorization_servers": ["https://as.example"]},
            prm_url: {"resource": "https://mcp.example", "authorization_servers": ["https://as.example"]},
        },
    )
    assert PRM_URL in seen
    assert not any("oauth-authorization-server" in url for url in seen), seen
    assert seen[-1] == "https://as.example/register"


async def test_a_partial_endpoint_block_is_not_a_document(_fixed_callback):
    """Two of the three required fields is not an RFC 8414 document, and the
    third is not guessable -- that is what discovery is for."""

    provider = await mcp_oauth.provider_for(
        "srv", _server_cfg(issuer="https://as.example", token_endpoint="https://as.example/token")
    )
    assert not _is_seeded(provider)


def test_delete_credentials_takes_the_lock_sidecar_with_it(tmp_path, monkeypatch):
    """A deleted credential leaves no trace -- not even the lock file that
    once guarded its writes."""
    from raven.mcp import oauth

    monkeypatch.setattr(oauth, "_credentials_dir", lambda: tmp_path)
    path = oauth.credentials_path("example")
    lock = path.parent / ".lock" / (path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    lock.write_text("")

    oauth.delete_credentials("example")

    assert not path.exists()
    assert not lock.exists()
    oauth.delete_credentials("example")


async def test_concurrent_first_use_binds_one_listener_and_one_uri(monkeypatch):
    """[race] apply gathers its connect attempts, so two OAuth servers on a
    process's first apply reach the endpoint bootstrap together. Unlocked,
    each bound its own listener and registered a different redirect URI --
    the exact port drift the fixed port exists to prevent."""
    import asyncio

    monkeypatch.setattr(mcp_oauth, "_callback_base", None)
    monkeypatch.setattr(mcp_oauth, "_fallback_runner", None)

    started: list[int] = []

    class _Site:
        def __init__(self, _runner, _host, port):
            self._port = port

        async def start(self):
            await asyncio.sleep(0)
            started.append(self._port)

    class _Runner:
        def __init__(self, _app):
            pass

        async def setup(self):
            await asyncio.sleep(0)

    monkeypatch.setattr("aiohttp.web.TCPSite", _Site)
    monkeypatch.setattr("aiohttp.web.AppRunner", _Runner)

    a, b = await asyncio.gather(
        mcp_oauth._ensure_callback_endpoint(),
        mcp_oauth._ensure_callback_endpoint(),
    )

    assert a == b, "two first-use connects must agree on one redirect URI"
    assert started == [mcp_oauth.CALLBACK_PORT], "one listener, not one per racer"
