"""OAuth 2.1 for remote MCP servers — the three pieces the SDK leaves to us.

``mcp.client.auth.OAuthClientProvider`` implements the whole spec-side flow
(RFC 9728 resource discovery, RFC 8414 AS metadata discovery, RFC 7591
dynamic client registration, PKCE, token refresh). What it delegates to the
host, and what this module provides:

- token/client persistence  -> :class:`FileTokenStorage`
  (``~/.raven/credentials/mcp/<server>.json``, 0600, atomic writes)
- opening the browser        -> :class:`_Flow` (also notifies the GUI over
  the ``oauth.pending`` / ``oauth.done`` events)
- receiving the redirect     -> a module-level pending map keyed by the
  OAuth ``state``; the gateway's ``/oauth/callback`` route (or the
  self-hosted fallback listener) calls :func:`resolve_callback`

The pending map and callback base are module-global on purpose: the loopback
redirect URL is a process-wide resource (``serve_commands.SERVE`` follows the
same pattern). The ``state`` value is read from the authorization URL the SDK
hands to ``redirect_handler`` — the SDK generates it, we only correlate.

A server's config may also carry the discovery results themselves
(``MCPOAuthConfig``, filled in by a market install from the catalog entry). When
it does, :func:`provider_for` seeds them instead of fetching them, so the
browser opens without the RFC 9728/8414 round trips in front of it -- and with a
pre-registered ``client_id``, without the registration round trip either. An
endpoint the server answers as not being there is disarmed and the next connect
discovers; see :class:`_CatalogSeed` and :data:`NO_SEED_ENV`.

The consumer is :class:`~raven.mcp.manager.MCPConnectionManager`:
for a server configured ``auth="oauth"`` it builds a provider through
:func:`provider_for` and hands it to the transport as an ``httpx.Auth``, so the
SDK runs the browser flow *inside* the connect. A connect that still fails is
classified by :func:`is_auth_error` and parked as ``auth_required`` --
deliberately not retried, until an explicit ``connect()`` (``plug.auth``).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple
from urllib.parse import parse_qs, urlparse

import portalocker
from loguru import logger

OAUTH_FLOW_TIMEOUT = 900.0
"""How long a browser authorization may stay pending before the connect
fails into ``auth_required``.

Fifteen minutes rather than the SDK provider's five. The clock starts when the
browser opens, and a first authorization is not one click: the user may have no
account with the provider yet, may be signed out, may have to pick a workspace,
and may be reading the scope list. Five minutes expired *mid-consent* often
enough to be the ordinary outcome rather than the exceptional one, and what it
costs to be wrong in this direction is one parked connect nobody is waiting on."""

_CALLBACK_PATH = "/oauth/callback"


class OAuthWaitTimeoutError(Exception):
    """The user never completed the browser flow in time."""


class OAuthUnavailableError(Exception):
    """No callback endpoint could be provisioned for the redirect."""


# ── Callback endpoint (process-wide) ───────────────────────────────

_callback_base: str | None = None
_fallback_runner: Any = None


class _Pending(NamedTuple):
    """One in-flight browser round-trip.

    ``url`` rides along because the authorization URL is otherwise reachable
    only by whoever received the ``oauth.pending`` event: an in-process caller
    that kicked the connect and now has to *tell someone* where to click (the
    agent's ``plugin`` tool) has no event stream to read.
    """

    server: str
    future: asyncio.Future
    url: str


_PENDING: dict[str, _Pending] = {}
"""OAuth ``state`` -> the pending round-trip it will resolve."""


def set_callback_base(base_url: str) -> None:
    """Declare the running HTTP origin that serves ``/oauth/callback``.

    No longer called by ``raven serve``: see :data:`CALLBACK_PORT` for why the
    redirect must not follow a port that moves. Kept for a host that genuinely
    owns a fixed origin and wants the callback on it.
    """
    global _callback_base
    _callback_base = base_url.rstrip("/")


def redirect_uri() -> str | None:
    return f"{_callback_base}{_CALLBACK_PATH}" if _callback_base else None


CALLBACK_PORT = int(os.environ.get("RAVEN_OAUTH_CALLBACK_PORT") or 18860)
"""First choice of loopback port for the OAuth redirect.

The redirect URI is part of the registration an authorization server stores, so
it has to survive restarts -- and it used to be the gateway's own port, which
does not: ``serve`` probes forward when its preferred port is taken, and
``FileTokenStorage.get_client_info`` correctly refuses a registration whose
redirect no longer matches. The consequence was silent and expensive: a port
change re-ran dynamic client registration, the new client could not use the
tokens minted for the old one, and a plugin the user had already authorized
asked to be authorized again -- opening a browser to do it.

A dedicated port, unrelated to whatever the page is served on, keeps one
registration valid for the life of the install."""

CALLBACK_PORT_TRIES = 8
"""How many consecutive ports to try before giving up on a stable one.

A single fixed port is only stable until something else claims it, and then the
ephemeral fallback re-registers on every start -- the very bug the fixed port
exists to prevent, back again and now silent. This happened immediately: the
first port picked was already held by a long-running process on the author's
machine. Walking a short deterministic ladder instead means a collision costs
one re-registration rather than one per launch, because the port that answers
today answers tomorrow too."""


async def _ensure_callback_endpoint() -> str:
    """Return the redirect URI, self-hosting the loopback listener on first use.

    Every process does this -- gateway, TUI and CLI alike -- so the registered
    redirect is the same string whatever raven is running as.
    """
    global _callback_base, _fallback_runner
    if _callback_base:
        return f"{_callback_base}{_CALLBACK_PATH}"

    from aiohttp import web

    async def _handler(request: "web.Request") -> "web.Response":
        ok, html = resolve_callback(dict(request.query))
        return web.Response(
            text=html,
            content_type="text/html",
            status=200 if ok else 400,
            headers={"Content-Security-Policy": CALLBACK_CSP, "X-Content-Type-Options": "nosniff"},
        )

    app = web.Application()
    app.router.add_get(_CALLBACK_PATH, _handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = None
    for candidate in range(CALLBACK_PORT, CALLBACK_PORT + CALLBACK_PORT_TRIES):
        try:
            site = web.TCPSite(runner, "127.0.0.1", candidate)
            await site.start()
        except OSError:
            continue
        port = candidate
        if candidate != CALLBACK_PORT:
            # Worth saying: the ladder is deterministic, so this stays true on
            # the next launch -- but it explains the one re-authorization the
            # move costs, and names the port to free if that is unwanted.
            logger.info("MCP OAuth: callback port {} is taken; using {} instead", CALLBACK_PORT, candidate)
        break
    if port is None:
        # Every candidate is held. An ephemeral port still completes the flow
        # that is running now; it only costs this registration its stability,
        # which beats failing the authorization outright.
        logger.warning(
            "MCP OAuth: ports {}-{} are all taken; falling back to an ephemeral one, "
            "which will force a re-registration on every start until one frees up",
            CALLBACK_PORT,
            CALLBACK_PORT + CALLBACK_PORT_TRIES - 1,
        )
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001 — aiohttp has no public accessor
    _fallback_runner = runner
    _callback_base = f"http://127.0.0.1:{port}"
    logger.info("MCP OAuth: callback listener on {}", _callback_base)
    return f"{_callback_base}{_CALLBACK_PATH}"


def resolve_callback(query: dict[str, Any]) -> tuple[bool, str]:
    """Resolve a redirect hitting ``/oauth/callback``.

    Returns ``(matched, html_page)``. Unknown/consumed ``state`` values get
    a failure page — a one-shot future can never be redeemed twice.
    """
    state = str(query.get("state") or "")
    entry = _PENDING.pop(state, None)
    if entry is None:
        return False, _page(False, "This authorization link is stale or was already used.")
    server, fut = entry.server, entry.future
    if fut.done():
        return False, _page(False, "This authorization was already completed.")
    error = query.get("error")
    if error:
        desc = str(query.get("error_description") or error)
        fut.set_exception(OAuthWaitTimeoutError(f"authorization denied: {desc}"))
        return True, _page(False, f"Authorization was denied ({desc}). You can close this page.")
    code = str(query.get("code") or "")
    if not code:
        fut.set_exception(OAuthWaitTimeoutError("authorization response carried no code"))
        return True, _page(False, "The authorization response was malformed. You can close this page.")
    fut.set_result((code, state))
    return True, _page(True, f"Raven is now connected to {server}. You can close this page.")


# Every callback response carries this. The page is reachable without a session
# by design -- the browser arrives from the authorization server -- and it shares
# an origin with /rpc, so script running here would be script running against the
# agent runtime. Nothing on this page needs to execute, so nothing may.
CALLBACK_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def _page(ok: bool, message: str) -> str:
    """Render the callback result page.

    ``message`` reaches here from the redirect's query string on the failure
    path (``error_description`` is whatever the authorization server sent), so
    it is escaped rather than trusted: an unescaped value would be reflected
    HTML on the same origin as /rpc.
    """
    import html

    tone = "#7fbf6a" if ok else "#d96a5b"
    title = "Authorization complete" if ok else "Authorization failed"
    safe = html.escape(message)
    return (
        "<!doctype html><meta charset='utf-8'><title>Raven</title>"
        "<body style='display:flex;align-items:center;justify-content:center;height:96vh;"
        "background:#16150f;color:#f7f2e4;font:15px/1.6 -apple-system,sans-serif'>"
        f"<div style='text-align:center'><div style='font-size:34px;color:{tone}'>{'✓' if ok else '✕'}</div>"
        f"<h1 style='font-size:17px;margin:10px 0 6px'>{title}</h1>"
        f"<p style='color:#a39b82;max-width:26em'>{safe}</p></div>"
    )


# ── Token / client persistence ─────────────────────────────────────


def _credentials_dir() -> Path:
    from raven.config.paths import get_runtime_subdir

    d = get_runtime_subdir("credentials") / "mcp"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def credentials_path(server: str) -> Path:
    return _credentials_dir() / f"{server}.json"


def delete_credentials(server: str) -> None:
    credentials_path(server).unlink(missing_ok=True)


class FileTokenStorage:
    """``mcp.client.auth.TokenStorage`` backed by one JSON file per server.

    ``get_client_info`` returns ``None`` when the stored registration's
    redirect URIs don't include the current callback URL (serve's port
    drifted) — the SDK then re-runs dynamic client registration; tokens are
    kept and revalidated by the normal 401/refresh path.
    """

    def __init__(self, server: str, redirect_uri: str) -> None:
        self._path = credentials_path(server)
        self._redirect_uri = redirect_uri
        self._preregistered: Any = None

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("MCP OAuth: unreadable credentials at {} ({})", self._path, e)
            return {}

    def _write(self, data: dict) -> None:
        lock = self._path.with_suffix(".lock")
        with portalocker.Lock(lock, mode="a+", timeout=30):
            fd, tmp = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=True, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:  # noqa: BLE001 — corrupt tokens mean "not authorized", not a crash
            return None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # ``expires_in`` is relative to the moment the token was minted, and
        # that moment dies with the process: the SDK restores the tokens on the
        # next start but not their expiry, treats "no expiry" as "still valid",
        # and rides the stale access token into a 401 -- whose handler is a full
        # browser authorization, not the silent refresh the stored
        # refresh_token was for. Anchor the deadline so provider_for can seed
        # it back (see stored_token_expiry).
        expires_in = getattr(tokens, "expires_in", None)
        if expires_in is not None:
            data["expires_at"] = time.time() + float(expires_in)
        else:
            data.pop("expires_at", None)
        self._write(data)

    def stored_token_expiry(self) -> float | None:
        """Absolute expiry of the stored tokens, for re-seeding the SDK.

        ``None`` means "do not seed": no tokens, or tokens that declared no
        expiry. A file written before ``expires_at`` existed but holding a
        refresh_token returns 1.0 -- long expired -- so the first request
        refreshes instead of trusting an access token of unknown age. Not 0.0:
        the SDK's ``is_token_valid`` tests ``not self.token_expiry_time``, so a
        falsy expiry reads as "no expiry, still valid" and the heal never fires.
        """
        data = self._read()
        tokens = data.get("tokens")
        if not tokens:
            return None
        expires_at = data.get("expires_at")
        if expires_at is not None:
            try:
                return float(expires_at)
            except (TypeError, ValueError):
                return None
        if tokens.get("refresh_token"):
            return 1.0
        return None

    def use_preregistered_client(self, client_info) -> None:
        """Stand in for dynamic registration when the file holds none.

        A registration this storage never minted, so it is never written back:
        it is a catalog fact, and a copy on disk would outlive the entry that
        justified it.
        """
        self._preregistered = client_info

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        if not raw:
            return self._preregistered
        try:
            info = OAuthClientInformationFull.model_validate(raw)
        except Exception:  # noqa: BLE001
            return self._preregistered
        if self._redirect_uri not in [str(u) for u in info.redirect_uris or []]:
            # Port drift. A registration raven minted is dead the moment its
            # redirect stops matching, but a pre-registered one is declared
            # against a redirect of its own and was only accepted because that
            # redirect is the one in use -- so it is the better fallback here
            # than re-running registration.
            return self._preregistered
        return info

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def seed_disarmed(self, fingerprint: str) -> bool:
        """Whether these exact catalog facts already failed against this server."""
        return str(self._read().get("oauth_seed_stale") or "") == fingerprint

    def disarm_seed(self, fingerprint: str) -> None:
        """Record that these catalog facts do not work, so the next connect
        discovers instead of trusting them again.

        Keyed by fingerprint rather than a bare flag: a catalog that later
        corrects the entry must be believed, and only the facts that actually
        failed stay distrusted.
        """
        try:
            data = self._read()
            if data.get("oauth_seed_stale") == fingerprint:
                return
            data["oauth_seed_stale"] = fingerprint
            self._write(data)
        except Exception as e:  # noqa: BLE001 — losing the marker costs the optimization, not the connect
            logger.warning("MCP OAuth: could not record a stale catalog seed at {}: {}", self._path, e)


# ── Browser flow ───────────────────────────────────────────────────


class _Flow:
    """redirect/callback handler pair for ONE connect attempt.

    ``interactive`` is whether a person asked for this connect *and is at this
    host's screen*. Only an explicit request from the local surface
    (``plug.install`` / ``plug.auth``) may take over the browser. Everything
    else -- the lazy connect every turn runs, a config reload, and the agent's
    ``plugin`` tool answering a request that arrived over a channel -- still
    mints the URL and publishes it, but leaves the opening to the user.
    """

    def __init__(
        self,
        server: str,
        notify: Callable[[str, dict], None] | None,
        *,
        interactive: bool = False,
    ) -> None:
        self._server = server
        self._notify = notify
        self._interactive = interactive
        self._state: str | None = None

    def _emit(self, event: str, payload: dict) -> None:
        if self._notify is None:
            return
        try:
            self._notify(event, payload)
        except Exception as e:  # noqa: BLE001 — a broken listener must not break the flow
            logger.warning("MCP OAuth: notify failed for '{}': {}", self._server, e)

    async def redirect(self, auth_url: str) -> None:
        state = parse_qs(urlparse(auth_url).query).get("state", [""])[0]
        if not state:
            raise OAuthUnavailableError("authorization URL carries no state parameter")
        # Register before the browser opens: the user can be faster than us.
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        _PENDING[state] = _Pending(self._server, fut, auth_url)
        self._state = state
        # The deadline travels with the invitation. A page that knows only "an
        # authorization is pending" cannot tell a flow still worth finishing
        # from one that expired, and shows the same thing for both.
        self._emit(
            "oauth.pending",
            {
                "server": self._server,
                "url": auth_url,
                "expires_in": OAUTH_FLOW_TIMEOUT,
                "interactive": self._interactive,
            },
        )
        if not self._interactive:
            # Nobody at this screen asked. Taking it for a third-party sign-in
            # the user did not initiate is disproportionate, and on a gateway the
            # host running this is not even the machine they are sitting at --
            # which holds for a request relayed through a turn too, however
            # deliberate the person on the far end was. The URL went out on the
            # event above; whoever is talking to them offers it, they choose.
            logger.info(
                "MCP OAuth: '{}' needs authorization; not opening a browser for a background connect",
                self._server,
            )
            return
        open_browser(self._server, auth_url)

    async def callback(self) -> tuple[str, str | None]:
        state = self._state
        if state is None or state not in _PENDING:
            raise OAuthUnavailableError("no pending authorization for this flow")
        fut = _PENDING[state].future
        try:
            code, got_state = await asyncio.wait_for(fut, timeout=OAUTH_FLOW_TIMEOUT)
        except asyncio.TimeoutError as e:
            _PENDING.pop(state, None)
            self._emit("oauth.done", {"server": self._server, "ok": False, "error": "timeout"})
            raise OAuthWaitTimeoutError(
                f"authorization for '{self._server}' timed out after {OAUTH_FLOW_TIMEOUT:.0f}s"
            ) from e
        except OAuthWaitTimeoutError as e:
            self._emit("oauth.done", {"server": self._server, "ok": False, "error": str(e)})
            raise
        self._emit("oauth.done", {"server": self._server, "ok": True})
        return code, got_state


def cancel_pending(server: str) -> None:
    """Invalidate any pending authorization for ``server``.

    Called when a new connect attempt supersedes a parked one (an explicit
    ``connect()`` on a server whose background attempt is waiting at the
    browser) and when the server is detached. The old link must stop being
    redeemable the moment a newer flow can be minted behind it: otherwise the
    first tab's success page completes an attempt whose epoch is already dead,
    its tokens land but its commit is dropped, and the newer attempt stays
    blocked on a callback that will never come.
    """
    for state in [s for s, entry in _PENDING.items() if entry.server == server]:
        fut = _PENDING.pop(state).future
        if not fut.done():
            fut.set_exception(OAuthWaitTimeoutError("superseded by a newer authorization attempt"))


def auth_wait_servers() -> set[str]:
    """Names of servers currently parked at the browser-authorization step.

    Lets callers that kick a connect (plug.install) stop waiting as soon as
    the flow reaches the browser: from that point the connect blocks on the
    user, not on the network.
    """
    return {entry.server for entry in _PENDING.values()}


def pending_url(server: str) -> str | None:
    """The authorization URL ``server`` is currently parked on, if any.

    For an in-process caller that has to hand the link to a person -- the agent
    tool answering "connect asana" cannot wait for the click, so the URL is the
    whole content of its answer. Event consumers get the same string on
    ``oauth.pending``; this is the pull side of it.
    """
    return next((entry.url for entry in _PENDING.values() if entry.server == server), None)


def open_browser(server: str, auth_url: str) -> bool:
    """Hand one authorization URL to the browser on *this* host.

    Reached only from an interactive :class:`_Flow`, which is what keeps the
    decision to open in one place. Nothing else may call it: a caller holding a
    URL for someone who is not at this screen -- the agent's ``plugin`` tool --
    reports the link instead.
    """
    import webbrowser

    try:
        opened = webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 — headless hosts have no browser; the URL is still reported
        opened = False
    if not opened:
        logger.info("MCP OAuth: open this URL to authorize '{}': {}", server, auth_url)
    return opened


# ── Catalog-carried authorization-server facts ─────────────────────

NO_SEED_ENV = "RAVEN_MCP_OAUTH_NO_SEED"
"""Set to force live discovery for every server, ignoring catalog facts.

The escape hatch for a catalog entry that is wrong in a way raven cannot
detect -- an authorization endpoint that has moved fails in the browser, where
nothing on this side sees the error."""

_ASM_WELL_KNOWN = ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration")
_PRM_WELL_KNOWN = "/.well-known/oauth-protected-resource"
_ENDPOINT_ABSENT = (404, 405)
"""Statuses that say a seeded URL is not the endpoint the entry claims it is."""


class _CatalogSeed:
    """The discovery documents a server's config already carries.

    Holds them in the shape the SDK parses off the wire, so the flow that
    consumes them is the same flow either way: :meth:`answer` hands back the
    document instead of letting the request leave the machine. What is missing
    from the config is simply not answered and still gets fetched.
    """

    def __init__(
        self,
        *,
        fingerprint: str,
        asm: dict | None,
        prm: dict | None,
        client_info: Any,
        hosts: set[str],
        token: str,
        register: str,
    ) -> None:
        self.fingerprint = fingerprint
        self.asm = asm
        self.prm = prm
        self.client_info = client_info
        self._hosts = hosts
        self._token = token
        self._register = register

    def answer(self, request: Any) -> Any:
        """The canned response for a discovery request, or ``None`` to let it fly."""
        import httpx

        if request.method != "GET":
            return None
        # Host-checked: the resource_metadata URL in a WWW-Authenticate header is
        # the server's to choose, and a document about some other origin is not
        # the one this config describes.
        if (request.url.host or "") not in self._hosts:
            return None
        path = request.url.path or ""
        if self.asm is not None and any(marker in path for marker in _ASM_WELL_KNOWN):
            return httpx.Response(200, json=self.asm, request=request)
        if self.prm is not None and _PRM_WELL_KNOWN in path:
            return httpx.Response(200, json=self.prm, request=request)
        return None

    def rejected(self, request: Any, response: Any) -> bool:
        """Whether ``response`` is this server refusing a seeded endpoint.

        A refusal at the registration endpoint is always about the entry: the
        only request that ever goes there is a registration the seeded metadata
        pointed at. The token endpoint also carries every routine refresh, and a
        service that has expired or revoked a ``refresh_token`` answers ``400
        invalid_grant`` -- disarming on that would retire the seed over a dead
        token rather than a wrong fact, so only a status that says the endpoint
        itself is not there counts there.

        An authorization endpoint that has moved is not covered -- that error is
        rendered in the user's browser, not returned here.
        """
        if response is None or response.status_code < 400:
            return False
        url = str(request.url)
        if self._register and url == self._register:
            return True
        return bool(self._token) and url == self._token and response.status_code in _ENDPOINT_ABSENT


def _seed_for(server: str, cfg: Any, uri: str, storage: FileTokenStorage) -> _CatalogSeed | None:
    """Read ``cfg.oauth`` into a seed, or return ``None`` to discover as usual."""
    if os.environ.get(NO_SEED_ENV, "").strip():
        return None
    oauth = getattr(cfg, "oauth", None)
    issuer = str(getattr(oauth, "issuer", "") or "")
    authorize = str(getattr(oauth, "authorization_endpoint", "") or "")
    token = str(getattr(oauth, "token_endpoint", "") or "")
    if not (issuer and authorize and token):
        # A partial document is not a document: the SDK's metadata model requires
        # all three, and guessing the rest is what discovery is for.
        return None

    register = str(oauth.registration_endpoint or "")
    scopes = [str(s) for s in (oauth.scopes or [])]
    asm: dict[str, Any] = {
        "issuer": issuer,
        "authorization_endpoint": authorize,
        "token_endpoint": token,
        "response_types_supported": ["code"],
    }
    if register:
        asm["registration_endpoint"] = register
    if scopes:
        asm["scopes_supported"] = scopes

    fingerprint = _fingerprint(oauth)
    if storage.seed_disarmed(fingerprint):
        logger.info("MCP OAuth: '{}' has failed with these catalog endpoints before; discovering instead", server)
        return None

    prm = _seeded_prm(server, cfg, oauth, issuer, scopes)
    client_info = _preregistered_client(server, oauth, uri)

    from urllib.parse import urlsplit

    hosts = {urlsplit(cfg.url).hostname or "", urlsplit(issuer).hostname or ""}
    return _CatalogSeed(
        fingerprint=fingerprint,
        asm=asm,
        prm=prm,
        client_info=client_info,
        hosts=hosts,
        token=token,
        register=register,
    )


def _fingerprint(oauth: Any) -> str:
    import hashlib

    payload = json.dumps(oauth.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _seeded_prm(server: str, cfg: Any, oauth: Any, issuer: str, scopes: list[str]) -> dict | None:
    """The RFC 9728 document to answer with, if the config's is safe to trust.

    Gated on the declared ``resource`` matching the audience the SDK would
    derive from the server URL on its own. A protected-resource document is the
    one discovery result whose staleness is *silent*: it fixes what audience the
    minted token is for, so a moved resource yields a token that looks fine and
    is rejected by every tool call. Refusing to move the audience keeps the
    worst a stale entry can do to a visible failure.
    """
    resource = str(getattr(oauth, "resource", "") or "")
    if not resource:
        return None
    try:
        from mcp.shared.auth_utils import resource_url_from_server_url
    except ImportError:  # pragma: no cover — SDK moved it; discovery still works
        return None
    canonical = resource_url_from_server_url(cfg.url)
    if resource.rstrip("/") != canonical.rstrip("/"):
        logger.info(
            "MCP OAuth: '{}' declares resource {} but its url canonicalizes to {}; fetching that document instead",
            server,
            resource,
            canonical,
        )
        return None
    prm: dict[str, Any] = {
        "resource": resource,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
    }
    if scopes:
        prm["scopes_supported"] = scopes
    return prm


def _preregistered_client(server: str, oauth: Any, uri: str) -> Any:
    """Build the declared client, or ``None`` to register dynamically.

    The redirect has to be the one raven is actually listening on. A registered
    client's redirect set is fixed at the service, and :data:`CALLBACK_PORT`
    walks a ladder when its first choice is taken -- so on the day the port
    moves, the pre-registered client would send the browser to a port nobody
    answers. Registering dynamically on the port in hand is the working
    alternative, and it costs one round trip.
    """
    client_id = str(getattr(oauth, "client_id", "") or "")
    if not client_id:
        return None
    declared = str(getattr(oauth, "redirect_uri", "") or "")
    if not declared:
        logger.warning(
            "MCP OAuth: '{}' declares a client_id but no redirect_uri; registering dynamically instead", server
        )
        return None
    if declared != uri:
        logger.info(
            "MCP OAuth: '{}' registered its client for {} but the callback is on {}; registering dynamically instead",
            server,
            declared,
            uri,
        )
        return None
    from mcp.shared.auth import OAuthClientInformationFull

    try:
        return OAuthClientInformationFull(
            client_id=client_id,
            redirect_uris=[declared],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — public client, not a secret
        )
    except Exception as e:  # noqa: BLE001 — an unusable declaration must not break the connect
        logger.warning(
            "MCP OAuth: '{}' declares an unusable client_id ({}); registering dynamically instead", server, e
        )
        return None


@lru_cache(maxsize=1)
def _seeded_provider_class(base: type) -> type:
    """``base`` with the discovery requests answered from a :class:`_CatalogSeed`.

    A subclass rather than a reimplementation: the SDK's ``async_auth_flow``
    stays the single description of the OAuth flow, and this only decides which
    of the requests it yields actually reach the network. That is what keeps the
    fallback honest -- a document the seed does not carry, or a seed that gets
    disarmed, leaves the flow byte-for-byte the one that runs today.
    """

    class SeededOAuthClientProvider(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, seed: _CatalogSeed, on_rejected: Callable[[], None], **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._seed = seed
            self._on_rejected = on_rejected

        async def async_auth_flow(self, request):  # type: ignore[no-untyped-def]
            inner = super().async_auth_flow(request)
            reply: Any = None
            try:
                while True:
                    try:
                        outgoing = await inner.asend(reply)
                    except StopAsyncIteration:
                        return
                    canned = self._seed.answer(outgoing)
                    if canned is not None:
                        reply = canned
                        continue
                    reply = yield outgoing
                    if self._seed.rejected(outgoing, reply):
                        self._on_rejected()
            finally:
                await inner.aclose()

    return SeededOAuthClientProvider


async def provider_for(
    server: str,
    cfg: Any,
    notify: Callable[[str, dict], None] | None = None,
    *,
    interactive: bool = False,
):
    """Build the SDK's OAuth provider for one server (an ``httpx.Auth``).

    Async because a process without a gateway (TUI/CLI) self-hosts the
    loopback callback listener on first use — the redirect URI must be
    final before the provider is built (DCR registers it).

    ``interactive`` says a person asked for this connect, and is the only thing
    that permits opening a browser. It defaults to False so a path that forgets
    to pass it is quiet rather than intrusive.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    uri = redirect_uri() or await _ensure_callback_endpoint()
    flow = _Flow(server, notify, interactive=interactive)
    storage = FileTokenStorage(server, uri)
    seed = _seed_for(server, cfg, uri, storage)
    kwargs: dict[str, Any] = {}
    cls: Any = OAuthClientProvider
    if seed is not None:
        if seed.client_info is not None:
            storage.use_preregistered_client(seed.client_info)
        cls = _seeded_provider_class(OAuthClientProvider)
        kwargs = {"seed": seed, "on_rejected": lambda: storage.disarm_seed(seed.fingerprint)}
    provider = cls(
        server_url=cfg.url,
        client_metadata=OAuthClientMetadata(
            client_name="Raven",
            redirect_uris=[uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — OAuth public-client mode, not a secret
        ),
        storage=storage,
        redirect_handler=flow.redirect,
        callback_handler=flow.callback,
        timeout=OAUTH_FLOW_TIMEOUT,
        **kwargs,
    )
    # The SDK's _initialize() restores the tokens but not when they expire, and
    # is_token_valid() reads a missing expiry as "valid" -- so a restart never
    # refreshes, it 401s and re-authorizes in a browser instead. Seed the expiry
    # we anchored at set_tokens time and the refresh branch works across
    # restarts; authorization is then an install-time event, not a recurring one.
    expiry = storage.stored_token_expiry()
    if expiry is not None:
        try:
            provider.context.token_expiry_time = expiry
        except AttributeError:  # pragma: no cover — SDK moved the field; refresh degrades, connect still works
            logger.warning("MCP OAuth: cannot seed token expiry for '{}'; SDK context changed shape", server)
    return provider


def is_auth_error(exc: BaseException) -> bool:
    """Whether a connect failure means "user must (re)authorize"."""
    import httpx
    from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError

    def check(e: BaseException, depth: int = 0) -> bool:
        if e is None or depth > 6:
            return False
        if isinstance(e, (OAuthFlowError, OAuthTokenError, OAuthRegistrationError, OAuthWaitTimeoutError)):
            return True
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return True
        if isinstance(e, BaseExceptionGroup):
            return any(check(sub, depth + 1) for sub in e.exceptions)
        return check(e.__cause__ or e.__context__, depth + 1)

    return check(exc)


__all__ = [
    "FileTokenStorage",
    "cancel_pending",
    "OAuthUnavailableError",
    "OAuthWaitTimeoutError",
    "credentials_path",
    "delete_credentials",
    "is_auth_error",
    "open_browser",
    "pending_url",
    "provider_for",
    "redirect_uri",
    "resolve_callback",
    "set_callback_base",
]
