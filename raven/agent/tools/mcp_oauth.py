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

The consumer is :class:`~raven.agent.tools.mcp_manager.MCPConnectionManager`:
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
from pathlib import Path
from typing import Any, Callable
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

_PENDING: dict[str, tuple[str, asyncio.Future]] = {}
"""OAuth ``state`` -> (server name, future resolving to (code, state))."""


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
    server, fut = entry
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
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        if not raw:
            return None
        try:
            info = OAuthClientInformationFull.model_validate(raw)
        except Exception:  # noqa: BLE001
            return None
        if self._redirect_uri not in [str(u) for u in info.redirect_uris or []]:
            return None  # port drift -> force re-registration
        return info

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


# ── Browser flow ───────────────────────────────────────────────────


class _Flow:
    """redirect/callback handler pair for ONE connect attempt.

    ``interactive`` is whether a person asked for this connect. Only an explicit
    one (``plug.install`` / ``plug.auth``) may take over the browser; a
    background connect -- the lazy one every turn runs, a config reload -- still
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
        _PENDING[state] = (self._server, fut)
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
            # Nobody asked. Taking the screen for a third-party sign-in the user
            # did not initiate is disproportionate, and on a gateway the host
            # running this is not even the machine they are sitting at. The URL
            # went out on the event above; the page offers it, they choose.
            logger.info(
                "MCP OAuth: '{}' needs authorization; not opening a browser for a background connect",
                self._server,
            )
            return
        import webbrowser

        try:
            opened = webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 — headless hosts have no browser; the GUI still shows the URL
            opened = False
        if not opened:
            logger.info("MCP OAuth: open this URL to authorize '{}': {}", self._server, auth_url)

    async def callback(self) -> tuple[str, str | None]:
        state = self._state
        if state is None or state not in _PENDING:
            raise OAuthUnavailableError("no pending authorization for this flow")
        _, fut = _PENDING[state]
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


def auth_wait_servers() -> set[str]:
    """Names of servers currently parked at the browser-authorization step.

    Lets callers that kick a connect (plug.install) stop waiting as soon as
    the flow reaches the browser: from that point the connect blocks on the
    user, not on the network.
    """
    return {server for server, _ in _PENDING.values()}


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
    return OAuthClientProvider(
        server_url=cfg.url,
        client_metadata=OAuthClientMetadata(
            client_name="Raven",
            redirect_uris=[uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # noqa: S106 — OAuth public-client mode, not a secret
        ),
        storage=FileTokenStorage(server, uri),
        redirect_handler=flow.redirect,
        callback_handler=flow.callback,
        timeout=OAUTH_FLOW_TIMEOUT,
    )


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
    "OAuthUnavailableError",
    "OAuthWaitTimeoutError",
    "credentials_path",
    "delete_credentials",
    "is_auth_error",
    "provider_for",
    "redirect_uri",
    "resolve_callback",
    "set_callback_base",
]
