"""Auth, path policy, and callback-escaping tests for the `raven serve` transport.

These cover the part of the gateway that decides who gets to talk to the agent
runtime at all. Everything here runs against a real aiohttp application built by
``build_app``, not against the handlers in isolation, because the interesting
failures live in the wiring: a route registered without its guard, a header
checked on one endpoint and not its neighbour, a page served without the CSP
that makes it safe to serve unauthenticated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.rpc.files import resolve_readable
from raven.rpc.transports.ws import WsGateway, build_app


@pytest.fixture
async def gateway_client(tmp_path: Path):
    """A live gateway with no static dir, plus its client."""
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    client = TestClient(server)
    await client.start_server()
    gateway.port = server.port
    try:
        yield gateway, client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# who may reach the authenticated surface
# ---------------------------------------------------------------------------


async def test_rpc_socket_rejects_a_client_with_no_credential(gateway_client) -> None:
    _, client = gateway_client
    resp = await client.get("/rpc")
    assert resp.status == 401


async def test_rpc_socket_rejects_a_wrong_cookie(gateway_client) -> None:
    _, client = gateway_client
    client.session.cookie_jar.update_cookies({"raven_session": "not-the-cookie"})
    resp = await client.get("/rpc")
    assert resp.status == 401


async def test_file_endpoint_rejects_a_client_with_no_credential(gateway_client) -> None:
    _, client = gateway_client
    resp = await client.get("/file", params={"path": "/etc/hosts"})
    assert resp.status == 401


async def test_health_and_auth_page_stay_open(gateway_client) -> None:
    """Two endpoints must answer before anyone holds a credential.

    ``/health`` is how a relauncher decides whether to reuse a running gateway,
    and ``/auth`` is the page that trades a nonce for one. Requiring a session
    for either would make signing in impossible.
    """
    _, client = gateway_client
    assert (await client.get("/health")).status == 200
    assert (await client.get("/auth")).status == 200


# ---------------------------------------------------------------------------
# the nonce is one-shot, time-boxed, and not mintable from a page
# ---------------------------------------------------------------------------


async def test_a_nonce_signs_in_once_and_only_once(gateway_client) -> None:
    gateway, client = gateway_client
    nonce = gateway.mint_nonce()

    first = await client.post("/auth/exchange", json={"nonce": nonce})
    assert first.status == 200
    assert (await client.get("/rpc")).status != 401, "the cookie from a burnt nonce must open /rpc"

    second = await client.post("/auth/exchange", json={"nonce": nonce})
    assert second.status == 403, "a redeemed nonce must not sign anyone in a second time"


async def test_an_expired_nonce_is_refused(gateway_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unredeemed nonce is a live credential; it must not stay live forever.

    One printed to the terminal because no browser opened would otherwise be
    redeemable for the life of the process, long after it scrolled away.
    """
    from raven.rpc.transports import ws as ws_module

    gateway, client = gateway_client
    clock = [1000.0]
    monkeypatch.setattr(ws_module.time, "monotonic", lambda: clock[0])

    nonce = gateway.mint_nonce()
    clock[0] += ws_module._NONCE_TTL_S + 1

    resp = await client.post("/auth/exchange", json={"nonce": nonce})
    assert resp.status == 403


async def test_minting_a_nonce_needs_the_shared_secret_not_a_cookie(gateway_client) -> None:
    """The mint endpoint is the one thing a signed-in page must not reach.

    A cookie holder that could mint would be able to issue itself fresh
    credentials indefinitely; keeping the mint on the header-only secret is what
    bounds a stolen cookie to the session it came from.
    """
    gateway, client = gateway_client
    nonce = gateway.mint_nonce()
    await client.post("/auth/exchange", json={"nonce": nonce})
    assert (await client.get("/rpc")).status != 401, "precondition: the cookie works"

    cookie_only = await client.post("/auth/nonce")
    assert cookie_only.status == 401, "a cookie must not be able to mint a nonce"

    with_secret = await client.post("/auth/nonce", headers={"X-Raven-Token": gateway.session_token})
    assert with_secret.status == 200
    assert (await with_secret.json())["nonce"]


async def test_the_browser_cookie_is_not_the_shared_secret(gateway_client) -> None:
    """Cookies ignore port (RFC 6265 section 8.5), so every other local service
    the user's browser touches receives this value. It must therefore be worth
    strictly less than the secret that mints credentials."""
    gateway, client = gateway_client
    nonce = gateway.mint_nonce()
    resp = await client.post("/auth/exchange", json={"nonce": nonce})

    cookie = resp.cookies["raven_session"].value
    assert cookie == gateway.session_cookie
    assert cookie != gateway.session_token

    posing_as_secret = await client.post("/auth/nonce", headers={"X-Raven-Token": cookie})
    assert posing_as_secret.status == 401, "the cookie must not work where the shared secret is required"


# ---------------------------------------------------------------------------
# origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["http://evil.example", "null"])
async def test_a_foreign_origin_is_refused(gateway_client, origin: str) -> None:
    gateway, client = gateway_client
    nonce = gateway.mint_nonce()
    resp = await client.post("/auth/exchange", json={"nonce": nonce}, headers={"Origin": origin})
    assert resp.status == 403


async def test_the_gateways_own_origin_is_accepted(gateway_client) -> None:
    gateway, client = gateway_client
    nonce = gateway.mint_nonce()
    resp = await client.post(
        "/auth/exchange",
        json={"nonce": nonce},
        headers={"Origin": f"http://127.0.0.1:{gateway.port}"},
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /oauth/callback: unauthenticated by design, so it may not run script
# ---------------------------------------------------------------------------


async def test_the_oauth_callback_escapes_what_the_redirect_carried(gateway_client) -> None:
    """``error_description`` is chosen by the authorization server, which is the
    remote side of a connection the user merely once agreed to. Reflected
    unescaped, it would be script on the same origin as /rpc -- and this route
    is deliberately reachable without a session.

    The state has to be a *live* one: an unknown state short-circuits to a fixed
    "stale link" page that never looks at the query, so testing with one would
    pass without the escaping ever running.
    """
    import asyncio

    from raven.mcp import oauth as mcp_oauth

    _, client = gateway_client
    payload = "<script>alert(1)</script>"
    state = "live-state-for-escaping-test"
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    mcp_oauth._PENDING[state] = mcp_oauth._Pending("example-server", fut, "https://idp.example/authorize")
    try:
        resp = await client.get(
            "/oauth/callback",
            params={"state": state, "error": "denied", "error_description": payload},
        )
        body = await resp.text()
    finally:
        mcp_oauth._PENDING.pop(state, None)
        if fut.done():
            fut.exception()  # retrieved, so asyncio does not warn on GC
        else:
            fut.cancel()

    # "alert(1)" survives escaping, so it proves the description reached the
    # page -- without it, a handler that dropped the value entirely would look
    # just as safe as one that escaped it.
    assert "alert(1)" in body, "precondition: the description must reach the page at all"
    assert "<script>" not in body, "reflected script on the same origin as /rpc"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


async def test_the_oauth_callback_forbids_script_even_if_something_slips_through(gateway_client) -> None:
    """Escaping is the fix; the CSP is what makes a future escaping bug survivable."""
    _, client = gateway_client
    resp = await client.get("/oauth/callback", params={"state": "unknown"})
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "sandbox" in csp
    assert "default-src 'none'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ---------------------------------------------------------------------------
# /file path policy
# ---------------------------------------------------------------------------


def _workspace_config(tmp_path: Path, *, restrict: bool):
    from raven.config.loader import load_config

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.tools.restrict_to_workspace = restrict
    return cfg


@pytest.mark.parametrize(
    "spelling",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "sub/../../../../etc/passwd",
    ],
)
def test_restricted_mode_refuses_every_way_out_of_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    from raven.rpc import files as files_module

    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(tmp_path, restrict=True))
    with pytest.raises(PermissionError):
        resolve_readable(spelling)


def test_restricted_mode_refuses_a_symlink_that_points_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check has to land on where a path resolves, not on how it is spelled:
    the agent can write into its own workspace, so it can plant the symlink."""
    from raven.rpc import files as files_module

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "escape.txt").symlink_to(outside)

    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(tmp_path, restrict=True))
    with pytest.raises(PermissionError):
        resolve_readable("escape.txt")


def test_raven_state_dir_is_refused_even_when_the_workspace_is_not_restricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restrict_to_workspace`` defaults to off, because the agent is meant to
    read the project it was pointed at. Raven's own state directory is not
    project data: it holds provider credentials and serve.json, whose token
    mints nonces. Serving that to a page would hand a cookie holder the secret
    the cookie is deliberately not worth."""
    from raven.rpc import files as files_module

    home = tmp_path / "raven-home"
    (home / "nested").mkdir(parents=True)
    (home / "serve.json").write_text(json.dumps({"token": "s3cret"}))
    (home / "nested" / "credentials.json").write_text("{}")
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(tmp_path, restrict=False))

    for target in (home / "serve.json", home / "nested" / "credentials.json"):
        with pytest.raises(PermissionError):
            resolve_readable(str(target))


def test_the_credential_store_is_refused_when_raven_home_points_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two state roots do not always agree, and both must be fenced.

    ``RAVEN_HOME`` moves serve.json and the tui runtime dir. It does not move
    the credential store: ``mcp_oauth._credentials_dir()`` hangs off
    ``get_config_path().parent``. Anchored on RAVEN_HOME alone, setting it left
    ~/.raven/credentials/mcp/<server>.json -- OAuth access and refresh tokens --
    as an ordinary path, served to any cookie holder with
    ``restrict_to_workspace`` off. Anchored on the data dir alone, serve.json
    would be the one exposed instead.
    """
    from raven.config import paths as paths_module
    from raven.rpc import files as files_module

    # The two roots, deliberately different -- which is the only state in which
    # a fence on one of them can be told apart from a fence on both.
    raven_home = tmp_path / "moved-home"
    (raven_home).mkdir()
    (raven_home / "serve.json").write_text(json.dumps({"token": "s3cret"}))

    data_dir = tmp_path / "instance"
    creds = data_dir / "credentials" / "mcp"
    creds.mkdir(parents=True)
    (creds / "github.json").write_text(json.dumps({"refresh_token": "r3fresh"}))

    monkeypatch.setenv("RAVEN_HOME", str(raven_home))
    monkeypatch.setattr(paths_module, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(tmp_path, restrict=False))

    for target in (raven_home / "serve.json", creds / "github.json"):
        with pytest.raises(PermissionError):
            resolve_readable(str(target))


def test_an_ordinary_file_outside_the_workspace_still_reads_when_unrestricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state-dir denial must not quietly become a workspace jail: with the
    restriction off, a project file elsewhere on disk is still viewable."""
    from raven.rpc import files as files_module

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))
    elsewhere = tmp_path.parent / "notes.md"
    elsewhere.write_text("# hi")
    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(tmp_path, restrict=False))

    assert resolve_readable(str(elsewhere)) == elsewhere.resolve()


def test_the_default_workspace_is_exempt_from_the_state_dir_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default workspace lives AT ``~/.raven/workspace`` -- inside the state
    directory -- so the fence as first written denied every file the agent
    itself had produced. The secrets the fence exists for (config.json, oauth/,
    serve.json) are all siblings of the workspace, never inside it."""
    from raven.rpc import files as files_module

    home = tmp_path / "raven-home"
    ws = home / "workspace"
    ws.mkdir(parents=True)
    (ws / "report.md").write_text("# produced by the agent")
    (home / "serve.json").write_text(json.dumps({"token": "s3cret"}))
    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.setattr(files_module, "load_config", lambda: _workspace_config(ws, restrict=False))

    assert resolve_readable(str(ws / "report.md")) == (ws / "report.md").resolve()
    with pytest.raises(PermissionError):
        resolve_readable(str(home / "serve.json"))


async def test_the_sign_in_cookie_outlives_the_browser_session(gateway_client) -> None:
    """Without a max-age this is a session cookie: closing the browser signs the
    user out of a gateway that never went anywhere, and the page then reports a
    sign-in failure for a service that is up. The server side is durable across
    restarts, so nothing but this bounds how long a tab stays signed in."""
    from raven.rpc.transports.ws import _COOKIE_MAX_AGE_S

    gateway, client = gateway_client
    resp = await client.post("/auth/exchange", json={"nonce": gateway.mint_nonce()})

    morsel = resp.cookies["raven_session"]
    assert int(morsel["max-age"]) == _COOKIE_MAX_AGE_S
    # Still the same hardening it had; the lifetime is the only thing added.
    assert morsel["httponly"]
    assert morsel["samesite"] == "Strict"


# ---------------------------------------------------------------------------
# each ws connection carries its own declared surface
# ---------------------------------------------------------------------------


async def test_each_ws_connection_keeps_its_own_declared_surface(gateway_client) -> None:
    """Two clients on one dispatcher: the page declares itself in system.hello,
    the other says nothing. What each connection's later frames observe is its
    own declaration -- the whole point of binding identity to the connection
    rather than the process."""
    from raven.rpc.connection import declared_surface
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.system import register_system_methods

    gateway, client = gateway_client
    dispatcher = Dispatcher()
    register_system_methods(dispatcher)

    async def probe(params: dict) -> dict:
        return {"surface": declared_surface()}

    dispatcher.register("test.surface", probe)
    gateway.dispatcher = dispatcher

    auth = {"X-Raven-Token": gateway.session_token}
    ws_page = await client.ws_connect("/rpc", headers=auth)
    ws_anon = await client.ws_connect("/rpc", headers=auth)
    try:
        await ws_page.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "system.hello",
                "params": {"client_version": "0.1.0", "surface": "page"},
            }
        )
        assert "result" in await ws_page.receive_json()
        await ws_anon.send_json(
            {"jsonrpc": "2.0", "id": 1, "method": "system.hello", "params": {"client_version": "0.1.0"}}
        )
        assert "result" in await ws_anon.receive_json()

        await ws_page.send_json({"jsonrpc": "2.0", "id": 2, "method": "test.surface", "params": {}})
        await ws_anon.send_json({"jsonrpc": "2.0", "id": 2, "method": "test.surface", "params": {}})
        assert (await ws_page.receive_json())["result"] == {"surface": "page"}
        assert (await ws_anon.receive_json())["result"] == {"surface": None}
    finally:
        await ws_page.close()
        await ws_anon.close()
