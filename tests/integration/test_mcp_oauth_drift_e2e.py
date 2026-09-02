"""End-to-end OAuth against a loopback authorization server.

The unit tests drive a stand-in for the SDK provider, which cannot show what the
pinned SDK does with the state raven hands it. These run raven's real
``provider_for`` through the real ``mcp.client.auth`` flow against a real HTTP
authorization server, and assert on what reached the wire: whether a refresh was
attempted, whether a registration was minted, and whether a browser was needed.

The only thing standing in for a person is the click on the consent page.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aiohttp import web

from raven.config.schema import MCPOAuthConfig, MCPServerConfig
from raven.mcp import oauth as mcp_oauth

pytestmark = pytest.mark.asyncio

OLD_CID = "old-cid"
LIVE_PORT = 18860
DRIFTED_REDIRECT = "http://127.0.0.1:19999/oauth/callback"
LIVE_REDIRECT = f"http://127.0.0.1:{LIVE_PORT}/oauth/callback"


class _AuthServer:
    """Enough of an authorization server to refuse the wrong things."""

    def __init__(self) -> None:
        self.clients: dict[str, list[str]] = {OLD_CID: [DRIFTED_REDIRECT]}
        self.refresh_owner: dict[str, str] = {"rt-old": OLD_CID}
        self.access: dict[str, str] = {}
        self.codes: dict[str, str] = {}
        self.calls: list[dict] = []
        self.revoked: set[str] = set()
        self.insufficient_scope = False
        self.omit_expires_in = False
        self._n = 0

    def seed_client(self, client_id: str, redirects: list[str]) -> None:
        self.clients[client_id] = redirects

    def counts(self, kind: str, **match) -> int:
        return sum(1 for c in self.calls if c["call"] == kind and all(c.get(k) == v for k, v in match.items()))

    async def _register(self, request: web.Request) -> web.Response:
        body = await request.json()
        self._n += 1
        cid = f"dcr-{self._n}"
        redirects = [str(u) for u in body.get("redirect_uris") or []]
        self.clients[cid] = redirects
        self.calls.append({"call": "register", "client_id": cid, "redirect_uris": redirects})
        return web.json_response(
            {
                "client_id": cid,
                "redirect_uris": redirects,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_id_issued_at": 1700000000 + self._n,
            },
            status=201,
        )

    async def _authorize(self, request: web.Request) -> web.Response:
        q = request.query
        cid, redirect = q.get("client_id", ""), q.get("redirect_uri", "")
        registered = self.clients.get(cid)
        if registered is None or redirect not in registered:
            # What a conforming server does, and what the user then sees: an
            # error page where a login was expected.
            self.calls.append({"call": "authorize", "client_id": cid, "result": "invalid_redirect_uri"})
            return web.json_response({"error": "invalid_redirect_uri"}, status=400)
        code = secrets.token_urlsafe(8)
        self.codes[code] = cid
        self.calls.append({"call": "authorize", "client_id": cid, "result": "ok"})
        return web.Response(status=302, headers={"Location": f"{redirect}?code={code}&state={q.get('state', '')}"})

    async def _token(self, request: web.Request) -> web.Response:
        form = await request.post()
        grant, cid = form.get("grant_type", ""), str(form.get("client_id", ""))
        if grant == "refresh_token":
            token = str(form.get("refresh_token", ""))
            if token in self.revoked or self.refresh_owner.get(token) != cid:
                self.calls.append({"call": "token", "grant": grant, "client_id": cid, "result": "invalid_grant"})
                return web.json_response({"error": "invalid_grant"}, status=400)
            return self._issue(cid, grant, rotate_from=token)
        if grant == "authorization_code" and self.codes.pop(str(form.get("code", "")), None) == cid:
            return self._issue(cid, grant)
        self.calls.append({"call": "token", "grant": grant, "client_id": cid, "result": "invalid_grant"})
        return web.json_response({"error": "invalid_grant"}, status=400)

    def _issue(self, cid: str, grant: str, rotate_from: str | None = None) -> web.Response:
        self._n += 1
        at, rt = f"at-{self._n}", f"rt-{self._n}"
        self.access[at] = cid
        self.refresh_owner[rt] = cid
        if rotate_from:
            self.refresh_owner.pop(rotate_from, None)
        self.calls.append({"call": "token", "grant": grant, "client_id": cid, "result": "ok"})
        body = {"access_token": at, "refresh_token": rt, "token_type": "Bearer", "scope": "read"}
        if not self.omit_expires_in:
            body["expires_in"] = 3600
        return web.json_response(body)

    async def _resource(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if token in self.access:
            if self.insufficient_scope:
                self.calls.append({"call": "resource", "result": "403"})
                return web.json_response(
                    {"error": "insufficient_scope"},
                    status=403,
                    headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="read write"'},
                )
            self.calls.append({"call": "resource", "result": "200"})
            return web.json_response({"ok": True})
        self.calls.append({"call": "resource", "result": "401"})
        return web.json_response(
            {"error": "unauthorized"}, status=401, headers={"WWW-Authenticate": 'Bearer realm="OAuth", scope="read"'}
        )

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/register", self._register)
        app.router.add_get("/authorize", self._authorize)
        app.router.add_post("/token", self._token)
        app.router.add_get("/mcp", self._resource)
        return app


class _Browser:
    """Stands in for the person. Records whether it was needed at all."""

    def __init__(self) -> None:
        self.opened = 0
        self.error: str | None = None
        self._result: tuple[str, str] | None = None

    async def redirect(self, url: str) -> None:
        self.opened += 1
        async with httpx.AsyncClient(follow_redirects=False) as c:
            r = await c.get(url)
        if r.status_code != 302:
            self.error = f"{r.status_code} {r.text[:100]}"
            return
        q = parse_qs(urlsplit(r.headers["Location"]).query)
        self._result = (q["code"][0], q.get("state", [""])[0])

    async def callback(self) -> tuple[str, str]:
        if self._result is None:
            raise RuntimeError(f"authorization did not reach a login: {self.error}")
        return self._result


def _write_drifted_credentials(path: Path, *, refresh_token: str | None, expires_in: float) -> None:
    """The starting state: a registration minted for a port that has moved."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {"access_token": "at-old", "token_type": "Bearer"}
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    path.write_text(
        json.dumps(
            {
                "client_info": {
                    "client_id": OLD_CID,
                    "redirect_uris": [DRIFTED_REDIRECT],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                "tokens": tokens,
                "expires_at": time.time() + expires_in,
            }
        )
    )


PUB_CLIENT = "pub-client"


def _catalog_client(cfg):
    """The same cfg with a catalog-declared client aimed at the live redirect."""
    return MCPServerConfig(
        url=cfg.url,
        auth="oauth",
        oauth=MCPOAuthConfig(
            **{**cfg.oauth.model_dump(exclude_none=True), "client_id": PUB_CLIENT, "redirect_uri": LIVE_REDIRECT}
        ),
    )


@pytest.fixture
async def drifted(tmp_path, monkeypatch):
    """A running authorization server plus a drifted credential on disk."""
    srv = _AuthServer()
    runner = web.AppRunner(srv.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # noqa: SLF001

    monkeypatch.setattr(mcp_oauth, "_credentials_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_oauth, "_callback_base", f"http://127.0.0.1:{LIVE_PORT}")
    monkeypatch.delenv(mcp_oauth.NO_SEED_ENV, raising=False)

    cfg = MCPServerConfig(
        url=f"{base}/mcp",
        auth="oauth",
        oauth=MCPOAuthConfig(
            issuer=base,
            authorization_endpoint=f"{base}/authorize",
            token_endpoint=f"{base}/token",
            registration_endpoint=f"{base}/register",
            scopes=["read"],
            resource=f"{base}/mcp",
        ),
    )
    try:
        yield SimpleNamespace(srv=srv, cfg=cfg, creds=tmp_path / "srv.json")
    finally:
        await runner.cleanup()


async def _connect(drifted) -> tuple[_Browser, httpx.Response | None, Exception | None]:
    provider = await mcp_oauth.provider_for("srv", drifted.cfg, interactive=True)
    browser = _Browser()
    provider.context.redirect_handler = browser.redirect
    provider.context.callback_handler = browser.callback
    try:
        async with httpx.AsyncClient(auth=provider, timeout=10) as client:
            return browser, await client.get(drifted.cfg.url), None
    except Exception as e:  # noqa: BLE001 — the assertion is about which failure
        return browser, None, e


async def test_a_scope_step_up_does_not_authorize_under_the_moved_client(drifted):
    """The SDK's other direct entry into authorization.

    A 403 carrying ``insufficient_scope`` calls ``_perform_authorization``
    without ever passing the registration check, so a drifted client installed
    at that moment goes straight onto the wire. A catalog-declared client is
    aimed at the redirect in use and recovers it; what must never happen is the
    moved one being offered to the server.
    """
    drifted.srv.seed_client(PUB_CLIENT, [LIVE_REDIRECT])
    drifted.srv.access["at-old"] = OLD_CID
    drifted.srv.insufficient_scope = True
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=3600)

    await _connect(SimpleNamespace(**{**vars(drifted), "cfg": _catalog_client(drifted.cfg)}))

    assert drifted.srv.counts("authorize", result="invalid_redirect_uri") == 0
    assert drifted.srv.counts("authorize", client_id=PUB_CLIENT, result="ok") == 1


async def test_a_scope_step_up_without_a_declared_client_puts_nothing_bad_on_the_wire(drifted):
    """With no client aimed at the live redirect there is nothing to recover
    with, and the step-up cannot register its way out. It must then fail without
    offering the moved client -- which is what the base branch did, and is a
    local failure rather than a browser opened onto an error page."""
    drifted.srv.access["at-old"] = OLD_CID
    drifted.srv.insufficient_scope = True
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=3600)

    browser, _, error = await _connect(drifted)

    assert error is not None
    assert drifted.srv.counts("authorize") == 0
    assert browser.opened == 0


async def test_a_restart_after_a_declared_client_takes_over_refreshes_under_it(drifted):
    """Ownership has to survive the process that established it.

    Handing authorization to the catalog client skips registration, so nothing
    writes the new owner to disk. Leaving the superseded one there has the next
    process pair the catalog client's tokens with a client_id that did not mint
    them, and its first refresh is `invalid_grant` -- a browser trip for a
    credential that was working.
    """
    drifted.srv.seed_client(PUB_CLIENT, [LIVE_REDIRECT])
    drifted.srv.revoked.add("rt-old")
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=-60)
    cfg = _catalog_client(drifted.cfg)

    first = SimpleNamespace(**{**vars(drifted), "cfg": cfg})
    _, response, error = await _connect(first)
    assert error is None and response is not None and response.status_code == 200

    stored = json.loads(drifted.creds.read_text())
    stored["expires_at"] = time.time() - 60
    drifted.creds.write_text(json.dumps(stored))

    browser, response, error = await _connect(first)

    assert error is None and response is not None and response.status_code == 200
    assert browser.opened == 0
    assert drifted.srv.counts("token", grant="refresh_token", client_id=PUB_CLIENT, result="ok") == 1
    assert drifted.srv.counts("token", grant="refresh_token", client_id=OLD_CID, result="invalid_grant") == 1


async def test_a_live_peer_refreshes_under_the_client_that_took_over(drifted):
    """The topology the coordination class exists for, after a handoff.

    The host keeps one provider while a bridged dispatch needs another. Only the
    one that performs the handoff learns the new owner; a peer that stays live
    would otherwise spend the replacement tokens under the client they replaced.
    """
    drifted.srv.seed_client(PUB_CLIENT, [LIVE_REDIRECT])
    drifted.srv.revoked.add("rt-old")
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=-60)
    ctx = SimpleNamespace(**{**vars(drifted), "cfg": _catalog_client(drifted.cfg)})

    peer = await mcp_oauth.provider_for("srv", ctx.cfg, interactive=True)
    peer_browser = _Browser()
    peer.context.redirect_handler = peer_browser.redirect
    peer.context.callback_handler = peer_browser.callback
    await peer._initialize()
    assert peer.context.client_info.client_id == OLD_CID

    _, response, error = await _connect(ctx)
    assert error is None and response is not None and response.status_code == 200

    stored = json.loads(drifted.creds.read_text())
    stored["expires_at"] = time.time() - 60
    drifted.creds.write_text(json.dumps(stored))
    drifted.srv.calls.clear()

    async with httpx.AsyncClient(auth=peer, timeout=10) as client:
        assert (await client.get(ctx.cfg.url)).status_code == 200

    assert peer_browser.opened == 0
    assert drifted.srv.counts("token", grant="refresh_token", client_id=PUB_CLIENT, result="ok") == 1
    assert drifted.srv.counts("token", grant="refresh_token", client_id=OLD_CID) == 0


async def test_a_server_that_omits_expires_in_is_refreshed_once_not_per_request(drifted):
    """Omitting ``expires_in`` is legal and usual for long-lived tokens.

    Recorded as an absent anchor it was indistinguishable from a file written
    before the anchor existed, so every flow re-applied the unknown-age
    sentinel: refresh, another response without ``expires_in``, round again. On
    a server that rotates refresh tokens that is one rotation per request.
    """
    drifted.srv.seed_client(OLD_CID, [LIVE_REDIRECT])
    drifted.srv.omit_expires_in = True
    drifted.creds.parent.mkdir(parents=True, exist_ok=True)
    # The legacy shape the sentinel is for: a refresh token and no anchor at all.
    drifted.creds.write_text(
        json.dumps(
            {
                "client_info": {
                    "client_id": OLD_CID,
                    "redirect_uris": [LIVE_REDIRECT],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
                "tokens": {"access_token": "at-old", "refresh_token": "rt-old", "token_type": "Bearer"},
            }
        )
    )

    provider = await mcp_oauth.provider_for("srv", drifted.cfg, interactive=True)
    async with httpx.AsyncClient(auth=provider, timeout=10) as client:
        for _ in range(3):
            assert (await client.get(drifted.cfg.url)).status_code == 200

    assert drifted.srv.counts("token", grant="refresh_token") == 1
    assert "expires_at" in json.loads(drifted.creds.read_text())


async def test_a_drifted_registration_still_refreshes_silently(drifted):
    """The bug this exists for: an authorized server going quiet after a restart.

    The stored refresh token is bound to the stored client_id, and neither cares
    about the callback port. Re-registering instead mints a client the token does
    not belong to, and the credential is then unrecoverable without a browser.
    """
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=-60)

    browser, response, error = await _connect(drifted)

    assert error is None and response is not None and response.status_code == 200
    assert browser.opened == 0
    assert drifted.srv.counts("token", grant="refresh_token", client_id=OLD_CID, result="ok") == 1
    assert drifted.srv.counts("register") == 0
    assert json.loads(drifted.creds.read_text())["client_info"]["client_id"] == OLD_CID


async def test_a_401_on_a_valid_looking_token_re_registers_instead_of_failing(drifted):
    """The transition an entry-point guard cannot see.

    The stored anchor still calls the access token valid, so no refresh is
    attempted -- the server simply refuses it. Authorization has to run, and it
    must not run under a client registered for a callback that moved: a
    conforming server answers ``invalid_redirect_uri``, which is an error page
    with no way out but deleting the credential by hand.
    """
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=3600)

    browser, response, error = await _connect(drifted)

    assert error is None and response is not None and response.status_code == 200
    assert drifted.srv.counts("authorize", result="invalid_redirect_uri") == 0
    assert drifted.srv.counts("register") == 1
    assert drifted.srv.counts("token", grant="refresh_token") == 0


async def test_a_refused_refresh_recovers_through_a_fresh_registration(drifted):
    """The registration is spent, not merely stale: the browser flow still has to
    work, on a client minted for the port in hand."""
    _write_drifted_credentials(drifted.creds, refresh_token="rt-old", expires_in=-60)
    drifted.srv.revoked.add("rt-old")

    browser, response, error = await _connect(drifted)

    assert error is None and response is not None and response.status_code == 200
    assert drifted.srv.counts("token", grant="refresh_token", result="invalid_grant") == 1
    assert drifted.srv.counts("authorize", result="invalid_redirect_uri") == 0
    assert drifted.srv.counts("register") == 1
    assert browser.opened == 1
