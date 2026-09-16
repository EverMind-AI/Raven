"""The one shape the three hosted backends share.

A hosted memory service, seen from :class:`raven.contracts.memory.MemoryBackend`,
is a base URL, an API key and five REST calls: search, add, delete, read one
conversation back, and something cheap to prove the key works. Everything the
contract says about failure -- a raise costs the host one call, so prefer a
quiet ``[]`` / ``False`` -- is handled once here; a subclass only says what
its service's requests and responses look like.

Two decisions every subclass inherits:

- ``store`` returns ``True`` the moment the service *accepts* the batch. All
  three services extract memories asynchronously (Mem0 hands back an event id,
  Zep a task id, MemOS an ``async_mode`` acknowledgement) and the host calls
  ``store`` on the turn path, where nothing may wait a minute for extraction.
  Nobody here polls a status endpoint.
- The key is read once, environment first: ``<NAME>_API_KEY`` beats the
  ``api_key`` of the plugin's own slice, so a shell that exports one wins over
  a file that recorded another. Without a key nothing is sent -- ``recall`` is
  ``[]``, ``store`` is ``False``, ``health`` names the variable to set.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar

import httpx

from raven.contracts.memory import BackendHealth, HealthCheck, Memory
from raven.plugins import PluginContext

# The host abandons a recall at 5.0s (raven/context_engine/segments/memory.py);
# the client gives up first so the host never waits on a call it has dropped.
RECALL_TIMEOUT_S = 2.5
STORE_TIMEOUT_S = 10.0
HEALTH_TIMEOUT_S = 5.0

# Transport-level failure (refused, DNS, timeout): no HTTP status exists.
UNREACHABLE = 0


@dataclass(frozen=True)
class Call:
    """One request a subclass wants sent."""

    method: str
    path: str
    json: Any = None
    params: dict[str, Any] | None = None


@dataclass
class Reply:
    """What came back: an HTTP status (``UNREACHABLE`` when none) and the
    parsed body (``None`` when there was none or it was not JSON)."""

    status: int
    body: Any = None
    detail: str = field(default="")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def clamp_score(value: Any, default: float = 0.5) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, score))


def flatten_content(content: Any) -> str:
    """AgentLoop content is a string or a list of multimodal parts; the
    services take text."""
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")).strip()
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return content if isinstance(content, str) else str(content)


def iso_timestamp(value: Any) -> str | None:
    """A message's ``timestamp`` as ISO 8601, or ``None`` when it has none the
    services could use. ``build_turn`` stamps ISO; importers may hand a seconds
    epoch."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class CloudBackend:
    """Base of the three hosted backends; not a backend by itself."""

    NAME: ClassVar[str]
    DEFAULT_BASE_URL: ClassVar[str]
    ENV_KEY: ClassVar[str]
    SIGNUP_URL: ClassVar[str]
    ROLES: ClassVar[frozenset[str]] = frozenset({"user", "assistant"})

    def __init__(self, ctx: PluginContext, *, client: httpx.AsyncClient | None = None) -> None:
        self._config: dict[str, Any] = dict(ctx.config or {})
        self._logger = ctx.logger
        # Identity comes from the host, never from the slice: a second copy of
        # the owner id is how store and recall silently drift apart.
        self._user_id: str = ctx.services.user_id
        self._api_key: str = os.environ.get(self.ENV_KEY) or str(self._config.get("api_key") or "")
        self._base_url: str = str(self._config.get("base_url") or self.DEFAULT_BASE_URL).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._feedback_noop_logged = False

    # ── what a subclass fills in ─────────────────────────────────────

    def _auth_headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _recall_call(self, query: str, top_k: int) -> Call:
        raise NotImplementedError

    def _parse_hits(self, body: Any) -> list[Memory]:
        raise NotImplementedError

    def _store_call(self, session_id: str, messages: list[dict[str, Any]]) -> Call:
        raise NotImplementedError

    def _delete_call(self, memory_id: str, kind: str | None) -> Call | None:
        raise NotImplementedError

    def _session_call(self, session_id: str) -> Call:
        raise NotImplementedError

    def _parse_session(self, body: Any) -> list[Memory]:
        return self._parse_hits(body)

    def _health_call(self) -> Call:
        raise NotImplementedError

    async def _before_store(self, session_id: str) -> bool:
        """A service that needs a container created before the first write
        (Zep's thread) does it here. ``False`` fails the store."""
        return True

    def _effective_status(self, reply: Reply) -> int:
        """A service that reports failure inside a 200 body (MemOS) maps it
        onto the HTTP status the rest of this class understands."""
        return reply.status

    def _store_accepted(self, reply: Reply) -> bool:
        return reply.ok

    def _classify_health(self, status: int, reply: Reply) -> BackendHealth:
        if 200 <= status < 300:
            return BackendHealth(ready=True, checks=[HealthCheck(self.NAME, "ok", self._base_url)])
        if status in (401, 403):
            return self._missing(f"API key rejected ({status}); check {self.ENV_KEY}")
        if status == 429:
            return BackendHealth(
                ready=True,
                checks=[HealthCheck(self.NAME, "degraded", f"rate limited by {self._base_url}")],
            )
        if status == UNREACHABLE:
            return self._missing(f"cannot reach {self._base_url}: {reply.detail}")
        return self._missing(f"{self._base_url} answered {status}")

    def _missing(self, hint: str) -> BackendHealth:
        return BackendHealth(ready=False, checks=[HealthCheck(self.NAME, "missing", hint)])

    # ── the contract ─────────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        top_k: int,
    ) -> list[Memory]:
        # Flat services have one track: the agent-track call is answered
        # empty without a request, as is the caller bug of both-or-neither.
        if (user_id is None) == (agent_id is None) or agent_id is not None:
            return []
        if top_k <= 0 or not self._api_key:
            return []
        reply = await self._send(self._recall_call(query, top_k), timeout=RECALL_TIMEOUT_S)
        if self._effective_status(reply) != 200:
            self._warn("recall", reply)
            return []
        try:
            hits = self._parse_hits(reply.body)
        except Exception as exc:  # noqa: BLE001 - a malformed body costs this call, not the turn
            self._logger.warning("%s recall: unreadable response (%s)", self.NAME, exc.__class__.__name__)
            return []
        return hits[:top_k]

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        batch = self._normalize(messages)
        if not batch:
            # Nothing the service would keep: not a failure, and not a request.
            return True
        if not self._api_key:
            return False
        if not await self._before_store(session_id):
            return False
        reply = await self._send(self._store_call(session_id, batch), timeout=STORE_TIMEOUT_S)
        if not self._store_accepted(reply):
            self._warn("store", reply)
            return False
        return True

    async def feedback(self, signals: dict[str, Any]) -> None:
        if not self._feedback_noop_logged:
            self._feedback_noop_logged = True
            self._logger.debug("%s: feedback is a no-op for this backend", self.NAME)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()

    async def health(self) -> BackendHealth | None:
        if not self._api_key:
            return self._missing(f"no API key: set {self.ENV_KEY} or run `raven onboard`; keys at {self.SIGNUP_URL}")
        reply = await self._send(self._health_call(), timeout=HEALTH_TIMEOUT_S)
        return self._classify_health(self._effective_status(reply), reply)

    async def delete(self, memory_id: str, *, kind: str | None = None) -> bool:
        if not self._api_key:
            return False
        call = self._delete_call(memory_id, kind)
        if call is None:
            return False
        reply = await self._send(call, timeout=STORE_TIMEOUT_S)
        if not reply.ok:
            self._warn("delete", reply)
            return False
        return True

    async def recall_session(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[Memory]:
        if not self._api_key or agent_id is not None:
            return []
        reply = await self._send(self._session_call(session_id), timeout=RECALL_TIMEOUT_S)
        if self._effective_status(reply) != 200:
            return []
        try:
            return self._parse_session(reply.body)
        except Exception:  # noqa: BLE001
            return []

    # ── plumbing ─────────────────────────────────────────────────────

    def _normalize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages or ():
            if not isinstance(m, dict) or m.get("role") not in self.ROLES:
                continue
            content = flatten_content(m.get("content", ""))
            if not content.strip():
                continue
            row: dict[str, Any] = {"role": m["role"], "content": content}
            stamp = iso_timestamp(m.get("timestamp"))
            if stamp:
                row["timestamp"] = stamp
            out.append(row)
        return out

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    async def _send(self, call: Call, *, timeout: float) -> Reply:
        url = f"{self._base_url}/{call.path.lstrip('/')}"
        started = time.monotonic()
        try:
            response = await self._http().request(
                call.method,
                url,
                json=call.json,
                params=call.params,
                headers=self._auth_headers(),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            return Reply(UNREACHABLE, None, f"{exc.__class__.__name__} after {time.monotonic() - started:.1f}s")
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return Reply(response.status_code, body, response.reason_phrase or "")

    def _warn(self, op: str, reply: Reply) -> None:
        # The path and status are the whole story; headers and bodies stay out
        # of the log because the former carry the key.
        self._logger.warning("%s %s: %s %s", self.NAME, op, reply.status or "unreachable", reply.detail)


__all__ = [
    "HEALTH_TIMEOUT_S",
    "RECALL_TIMEOUT_S",
    "STORE_TIMEOUT_S",
    "UNREACHABLE",
    "Call",
    "CloudBackend",
    "Reply",
    "clamp_score",
    "flatten_content",
    "iso_timestamp",
]
