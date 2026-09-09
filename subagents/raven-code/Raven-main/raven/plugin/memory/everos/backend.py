"""EverosBackend — HTTP-only memory backend.

The backend is the host's :class:`MemoryBackend` implementation,
delegating to a running EverOS server over HTTP
(``POST /api/v1/memory/{search,add,...}``).

Constructor accepts an explicit ``adapter`` so tests can inject a
fake without monkeypatching module-level imports. Production wiring
goes through :func:`make_backend` -> ``EverosBackend(ctx)`` ->
``_make_http_adapter``.

Three architectural invariants worth re-stating:

1. **No compaction.** ``backend.store`` writes to EverOS's index and
   returns. raven core's ``MemoryConsolidator.maybe_consolidate`` is
   a separate post-turn step the host owns.
2. **No ``long_term`` property.** raven core's :class:`MemoryStore`
   stays where it is; Sentinel / Personalizer / ContextBuilder import
   it directly. The backend is unaware of MEMORY.md.
3. **recall names the track explicitly.** EverOS takes
   ``owner_type: Literal["user", "agent"]`` explicitly; the host passes
   ``user_id`` XOR ``agent_id`` and the backend forwards the set field
   straight to EverOS's :class:`SearchRequest`. Neither or both set
   logs a warning and recall returns ``[]``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import httpx

from raven.memory_engine import Memory
from raven.plugin import PluginContext
from raven.plugin.memory.everos.scope import DEFAULT_APP_ID, Scope, resolve_scope

logger = logging.getLogger("raven.plugin.memory.everos")

_OwnerType = Literal["user", "agent"]

_DEFAULT_AGENT_ID: str = "default"
_DEFAULT_USER_ID: str = "default"


# ---------------------------------------------------------------------------
# Adapter layer — swappable shim around the underlying EverOS
# ---------------------------------------------------------------------------


class _Adapter(Protocol):
    """Internal adapter contract — narrower than :class:`MemoryBackend`
    so the backend's translation layer (track routing, message
    shape conversion, result-list flattening) stays in one place.

    Two production implementations:

    - :class:`_HttpEverosAdapter` — HTTP client over EverOS's REST API.
    - :class:`_NoOpAdapter` — returns ``None`` / swallows writes.
      Used by tests that don't care about everos.

    The EverOS storage scope is deliberately absent from these
    signatures. It belongs to the adapter, not to a call: passing it
    per-call is what let a write address one bucket while the matching
    read addressed another.
    """

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
        session_id: str | None = None,
    ) -> Any: ...

    async def memorize(
        self,
        session_id: str,
        payload_messages: list[dict[str, Any]],
        *,
        is_final: bool = False,
    ) -> None: ...


class _NoOpAdapter:
    """Adapter that does nothing. Used as a graceful fallback so callers
    don't need a separate code path for "backend disabled"."""

    async def search(self, **kw: Any) -> Any:
        return None


    async def memorize(self, *a: Any, **kw: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# HTTP adapter
# ---------------------------------------------------------------------------


# Bounds for flattening the unextracted buffer tail into recall hits: one
# long turn must not flood the # Memory segment (the renderer adds bullets
# verbatim, without truncation).
_TAIL_MAX_MESSAGES = 10
_TAIL_MAX_CHARS = 500


def _tail_text(content: Any) -> str:
    """Text of one buffered message: the string form, or the joined text
    items of the multi-modal list form (non-text parts are dropped)."""
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for item in content or []:
        t = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
    return "\n".join(parts)


def _jsonify(obj: Any) -> Any:
    """Recursively turn parsed-JSON ``dict`` / ``list`` trees into
    nested :class:`SimpleNamespace` so the host's existing attribute-
    style access (``data.episodes[0].summary``) works on HTTP responses
    without importing EverOS's pydantic DTOs.

    Leaf values pass through unchanged. The conversion is small and
    cheap; profiling on a 50-item response shows < 0.5 ms.
    """
    if isinstance(obj, dict):
        return SimpleNamespace(
            **{k: _jsonify(v) for k, v in obj.items()},
        )
    if isinstance(obj, list):
        return [_jsonify(x) for x in obj]
    return obj


# The bundled everos (1.1.3) mounts its memory routes under /api/v1 only; the
# hosted service mounts /api/v2 only. Default to the server raven can start
# itself so the out-of-box path keeps working.
DEFAULT_API_VERSION = "v1"
SUPPORTED_API_VERSIONS = ("v1", "v2")

_DEFAULT_HTTP_TIMEOUT_S: float = 60.0
_MEMORIZE_TIMEOUT_S: float = 360.0


def _timestamp_ms(value: object, *, default_ms: int) -> int:
    """Coerce a message's timestamp to the ms epoch EverOS's DTO declares.

    Raven stamps its own records with an ISO-8601 string while
    ``MessageItemDTO.timestamp`` is an ``int``, so passing one straight
    through fails the whole write -- ``422 INVALID_INPUT`` over HTTP, and an
    ``int()`` parse error embedded. The failure is logged at warning level and
    nothing else reports it, so the store simply stops receiving turns.

    An unparseable value falls back to now rather than dropping the message:
    the timestamp is bookkeeping about the turn, the content is the part worth
    keeping.
    """
    if value is None or isinstance(value, bool):
        return default_ms
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return default_ms
    text = value.strip()
    if not text:
        return default_ms
    if text.isdigit():
        return int(text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return default_ms
    # Raven writes a naive isoformat() and its clock is UTC; reading it as
    # local time would shift every stored turn by the host's offset.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)

class _HttpEverosAdapter:
    """Adapter that talks to an EverOS service over HTTP.

    Endpoints, under a configurable API version prefix:

    - ``POST /api/<ver>/memory/search`` — request body ``SearchRequest``,
      response ``{request_id, data: SearchData}``.
    - ``POST /api/<ver>/memory/add`` — request body ``MemorizeAddRequest``,
      response ``{request_id, data: AddResponseData}``.
    - ``POST /api/<ver>/memory/flush`` — force extraction for a session.

    The version is a knob because the two deployments raven talks to do
    not overlap: the bundled server (everos 1.1.3, which raven can start
    itself) serves only ``v1``, and the hosted service serves only
    ``v2``. Guessing would turn a version mismatch into a 404 mid-turn,
    so it is declared -- and a 404 says which knob to change.

    The adapter constructs an :class:`httpx.AsyncClient` per-instance by
    default; tests inject a pre-built client (typically with
    ``httpx.MockTransport``) so no actual sockets open. Lifetime of an
    auto-built client is managed via :meth:`aclose` called from
    :meth:`EverosBackend.stop`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        scope: Scope | None = None,
        api_key: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api_version not in SUPPORTED_API_VERSIONS:
            raise ValueError(
                f"unsupported EverOS api_version {api_version!r}; "
                f"expected one of {sorted(SUPPORTED_API_VERSIONS)}"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_version = api_version
        self._scope = scope or Scope(app_id=DEFAULT_APP_ID, project_id="default")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
        )

    async def aclose(self) -> None:
        """Close the underlying client if we own it. Idempotent."""
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _url(self, endpoint: str) -> str:
        return f"{self._base_url}/api/{self._api_version}/memory/{endpoint}"

    def _raise_for_status(self, r: httpx.Response, endpoint: str) -> None:
        if r.status_code == 404:
            raise RuntimeError(
                f"EverOS at {self._base_url} has no {self._api_version} "
                f"memory/{endpoint} endpoint. The bundled server serves v1 and "
                f"the hosted service serves v2 -- set the everos-memory plugin's "
                f"'api_version' to match this deployment."
            )
        r.raise_for_status()

    def _scoped(self, body: dict[str, Any]) -> dict[str, Any]:
        """Stamp the storage scope onto an outbound request body.

        Every request this adapter sends goes through here, which is what
        makes reads and writes address the same bucket by construction.
        """
        body["app_id"] = self._scope.app_id
        body["project_id"] = self._scope.project_id
        return body

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
        session_id: str | None = None,
    ) -> Any:
        # Wire contract is user_id XOR agent_id (everos search route).
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if user_id is not None:
            body["user_id"] = user_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        if session_id is not None:
            # A top-level equality scalar, not wrapped in AND/OR or {"eq": ...}:
            # that is the only shape that also returns the not-yet-extracted
            # tail, which matters because extraction lags a turn by far longer
            # than a coding task takes.
            body["filters"] = {"session_id": session_id}
        r = await self._client.post(
            self._url("search"), json=self._scoped(body), headers=self._headers()
        )
        self._raise_for_status(r, "search")
        payload = r.json() or {}
        # Server returns ``{request_id, data: {episodes, profiles, ...}}``.
        # The backend's converter only needs ``data`` — extract + jsonify.
        data = payload.get("data", {})
        return _jsonify(data)

    async def memorize(
        self,
        session_id: str,
        payload_messages: list[dict[str, Any]],
        *,
        is_final: bool = False,
    ) -> None:
        body: dict[str, Any] = self._scoped(
            {
                "session_id": session_id,
                "messages": payload_messages,
            }
        )
        r = await self._client.post(
            self._url("add"), json=body, headers=self._headers(), timeout=_MEMORIZE_TIMEOUT_S
        )
        self._raise_for_status(r, "add")
        if is_final:
            fr = await self._client.post(
                self._url("flush"),
                json=self._scoped({"session_id": session_id}),
                headers=self._headers(),
                timeout=_MEMORIZE_TIMEOUT_S,
            )
            self._raise_for_status(fr, "flush")


# ---------------------------------------------------------------------------
# EverosBackend — host's MemoryBackend implementation
# ---------------------------------------------------------------------------


class EverosBackend:
    """raven.plugin.memory.everos's :class:`MemoryBackend` implementation."""

    def __init__(
        self,
        ctx: PluginContext,
        *,
        adapter: _Adapter | None = None,
    ) -> None:
        self._config = ctx.config
        self._services = ctx.services
        self._logger = ctx.logger
        self._agent_id: str = self._config.get("agent_id") or _DEFAULT_AGENT_ID
        self._user_id: str = self._config.get("user_id") or _DEFAULT_USER_ID
        # Default 0: no client-driven flush. Measured against the hosted
        # service, a flush issued right after add finds the buffer still
        # empty (ingestion is async) and extracts nothing -- extraction is
        # the server scheduler's job. Same-session recall does not depend
        # on it either: a session-filtered search already returns the
        # unextracted buffer tail. Set N>0 to force a flush every N turns
        # (extraction then lags at least one turn).
        self._flush_every_turns: int = int(
            self._config.get("flush_every_turns", 0),
        )
        # One coding task is one session, so a task recalling a sibling task's
        # memory is contamination, not context. Set false for an assistant that
        # should carry memory across conversations.
        self._scope_recall_to_session: bool = bool(
            self._config.get("scope_recall_to_session", True),
        )
        self._turn_counts: dict[str, int] = {}
        self._feedback_noop_logged = False
        # Resolved once here so both the store path and the recall path read
        # the same pair; there is no per-call override.
        self._scope = resolve_scope(
            getattr(self._services, "workspace", None),
            self._config,
        )

        if adapter is not None:
            self._adapter: _Adapter | None = adapter
        else:
            self._adapter = self._make_http_adapter()

    def _make_http_adapter(self) -> _Adapter:
        """Construct an :class:`_HttpEverosAdapter` from plugin config.

        Pulls ``base_url`` / ``api_key`` / ``timeout_s`` out of
        ``ctx.config`` with documented defaults.
        """
        base_url = self._config.get("base_url") or "http://localhost:18791"
        api_key = self._config.get("api_key") or os.environ.get("EVEROS_API_KEY") or None
        timeout_s = float(
            self._config.get("timeout_s", _DEFAULT_HTTP_TIMEOUT_S),
        )
        return _HttpEverosAdapter(
            base_url,
            scope=self._scope,
            api_key=api_key,
            api_version=str(self._config.get("api_version") or DEFAULT_API_VERSION),
            timeout_s=timeout_s,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        self._logger.info(
            "EverosBackend.start (adapter=%s)",
            type(self._adapter).__name__,
        )
        if isinstance(self._adapter, _HttpEverosAdapter):
            import sys

            if sys.platform == "win32":
                from rich.console import Console

                Console(stderr=True).print(
                    "[yellow]EverOS memory is not available on native Windows.[/yellow]\n"
                    "[dim]Run Raven inside WSL for full memory support, "
                    "or run `raven onboard` to reconfigure.[/dim]"
                )
                self._adapter = _NoOpAdapter()
                return

            from raven.plugin.memory.everos._server import (
                ensure_everos_server,
                probe_everos_server,
            )

            # A configured base_url names somebody else's service. Spawning a
            # local server on that port answers a different address than the
            # one we were told to use, so an unreachable remote is an error.
            configured_url = self._config.get("base_url")
            base_url = configured_url or "http://localhost:18791"
            try:
                if configured_url:
                    await probe_everos_server(base_url)
                else:
                    await ensure_everos_server(base_url)
            except Exception as e:
                self._logger.error(
                    "EverosBackend: EverOS at %s is unavailable (%s)",
                    base_url,
                    e,
                )
                raise
            self._logger.info(
                "EverosBackend ready: mode=%s base_url=%s root=%s app_id=%s project_id=%s flush_every_turns=%s",
                "remote" if configured_url else "local",
                base_url,
                os.environ.get("EVEROS_ROOT"),
                self._scope.app_id,
                self._scope.project_id,
                self._flush_every_turns,
            )

    async def stop(self) -> None:
        self._logger.info("EverosBackend.stop")
        aclose = getattr(self._adapter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as e:
                self._logger.warning(
                    "EverosBackend: adapter.aclose failed: %s",
                    e,
                )

    # ── MemoryBackend Protocol ─────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        top_k: int,
    ) -> list[Memory]:
        """Semantic recall via EverOS, scoped to one track.

        ``user_id`` set → everos ``user_id`` → episodes + profiles.
        ``agent_id`` set → everos ``agent_id`` → cases + skills.
        Exactly one must be set (XOR); neither or both → warn + empty.

        Transport failures propagate, as the MemoryBackend contract
        states. Swallowing them made an unreachable EverOS look exactly
        like a store with nothing relevant in it, so a run could believe
        memory was working while recalling nothing all session.
        """
        if (user_id is None) == (agent_id is None):
            self._logger.warning(
                "EverosBackend.recall: expected exactly one of user_id / "
                "agent_id (got user_id=%r, agent_id=%r); returning empty",
                user_id,
                agent_id,
            )
            return []
        owner_type: _OwnerType = "user" if user_id is not None else "agent"
        if self._adapter is None:
            return []  # adapter still building (start() not finished); degrade to no hits
        data = await self._adapter.search(
            user_id=user_id,
            agent_id=agent_id,
            query=query,
            top_k=top_k,
            session_id=session_id if self._scope_recall_to_session else None,
        )
        if data is None:
            return []
        return self._search_data_to_memories(data, owner_type)

    async def store(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Forward a turn's messages to EverOS for indexing.

        EverOS partitions internally by message sender (user-track vs
        agent-track); we don't need to specify ``owner_type`` here. We
        do need to convert from the host's
        ``{"role", "content", ...}`` shape to EverOS's
        ``MessageItemDTO`` shape (``sender_id`` + ``timestamp`` are
        required there, optional here).

        System messages are dropped — EverOS only accepts
        user/assistant/tool. Empty-text messages and empty payloads
        skip the adapter call entirely.
        """
        if not messages:
            return
        payload = self._convert_messages(
            messages,
            agent_id=self._agent_id,
            user_id=self._user_id,
        )
        if not payload:
            return
        if self._adapter is None:
            return
        if metadata:
            stray = {"app_id", "project_id"} & set(metadata)
            if stray:
                raise ValueError(
                    f"store() does not accept a per-call storage scope (got {sorted(stray)}); "
                    "the scope belongs to the backend so reads and writes cannot diverge"
                )
        if metadata and "is_final" in metadata:
            is_final = bool(metadata["is_final"])
        else:
            n = self._turn_counts.get(session_id, 0) + 1
            self._turn_counts[session_id] = n
            is_final = self._flush_every_turns > 0 and n % self._flush_every_turns == 0

        await self._adapter.memorize(
            session_id,
            payload,
            is_final=is_final,
        )

    async def feedback(self, signals: dict[str, Any]) -> None:
        """Deliberate no-op pending an upstream everos feedback sink.

        The host already collects ``skill_usage`` signals (which everos
        skills were injected / used in a turn) and dispatches them here.
        everos 1.0.0's service layer exposes no endpoint to consume them
        — ``agent_skill.confidence`` lives in the persistence internals
        with no service-level write path — so signals are dropped until
        everos grows one. The method stays on the Protocol because it is
        a valid optional capability and the host plumbing is in place;
        this is not dead code.

        Logged once at INFO so the pending wiring stays visible without
        flooding the per-turn after-turn pipeline.
        """
        if not self._feedback_noop_logged:
            self._feedback_noop_logged = True
            self._logger.info(
                "EverosBackend.feedback: no everos sink yet; skill_usage "
                "signals dropped (keys=%s). Logged once per backend.",
                sorted(signals.keys()),
            )
        else:
            self._logger.debug(
                "EverosBackend.feedback no-op (keys=%s)",
                sorted(signals.keys()),
            )

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _search_data_to_memories(
        data: Any,
        owner_type: _OwnerType,
    ) -> list[Memory]:
        """Flatten EverOS's typed result envelope into ``list[Memory]``.

        The host doesn't read backend-specific shapes — everything the
        prompt sees comes from ``Memory.text``. Per-row metadata (ids,
        confidence, source type) is preserved in ``Memory.metadata``
        so debug overlays / future telemetry can attribute.
        """
        out: list[Memory] = []
        if owner_type == "user":
            for ep in getattr(data, "episodes", None) or []:
                text = getattr(ep, "summary", "") or getattr(ep, "episode", "") or ""
                out.append(
                    Memory(
                        text=text,
                        score=float(getattr(ep, "score", 0.0) or 0.0),
                        metadata={
                            "id": ep.id,
                            "session_id": getattr(ep, "session_id", None),
                            "type": "episode",
                            "owner_type": "user",
                        },
                    )
                )
            for prof in getattr(data, "profiles", None) or []:
                out.append(
                    Memory(
                        text=_flatten_profile(prof.profile_data),
                        score=float(getattr(prof, "score", None) or 1.0),
                        metadata={
                            "id": prof.id,
                            "type": "profile",
                            "owner_type": "user",
                        },
                    )
                )
            # The unextracted buffer tail: raw messages still in the
            # boundary-detection buffer, returned only for a session-pinned
            # search. Extraction lags the conversation (hosted ingestion is
            # asynchronous; the bundled server extracts on flush/boundary),
            # so for a session-scoped run this tail IS the memory of what
            # just happened -- dropping it made pre-extraction recall
            # silently empty against a real server (measured on 1.1.3).
            # Tool messages are skipped (bulk, low fact density); the tail
            # is bounded because the renderer adds bullets verbatim.
            tail = [
                m
                for m in (getattr(data, "unprocessed_messages", None) or [])
                if getattr(m, "role", None) in ("user", "assistant")
            ]
            for msg in tail[-_TAIL_MAX_MESSAGES:]:
                text = _tail_text(getattr(msg, "content", ""))
                if not text:
                    continue
                out.append(
                    Memory(
                        text=text[:_TAIL_MAX_CHARS],
                        score=0.0,
                        metadata={
                            "id": getattr(msg, "id", None),
                            "session_id": getattr(msg, "session_id", None),
                            "type": "unprocessed",
                            "role": msg.role,
                            "owner_type": "user",
                        },
                    )
                )
        else:  # agent
            for skill in getattr(data, "agent_skills", None) or []:
                out.append(
                    Memory(
                        text=getattr(skill, "content", "") or "",
                        score=float(getattr(skill, "score", 0.0) or 0.0),
                        metadata={
                            "id": skill.id,
                            "name": getattr(skill, "name", ""),
                            "type": "skill",
                            "owner_type": "agent",
                            "confidence": getattr(skill, "confidence", None),
                        },
                    )
                )
            for case in getattr(data, "agent_cases", None) or []:
                # task_intent + key_insight makes a more useful prompt
                # bullet than task_intent alone.
                text = getattr(case, "task_intent", "") or ""
                insight = getattr(case, "key_insight", None)
                if insight:
                    text = f"{text}\n\n{insight}" if text else insight
                out.append(
                    Memory(
                        text=text,
                        score=float(getattr(case, "score", 0.0) or 0.0),
                        metadata={
                            "id": case.id,
                            "type": "case",
                            "owner_type": "agent",
                        },
                    )
                )
        out.sort(key=lambda m: m.score, reverse=True)
        return out

    @staticmethod
    def _convert_messages(
        messages: list[dict[str, Any]],
        *,
        agent_id: str,
        user_id: str = "default",
    ) -> list[dict[str, Any]]:
        """Adapt raven AgentLoop messages into EverOS's MessageItemDTO shape.

        AgentLoop: ``{"role", "content", ...}`` with role ∈ {"system",
        "user", "assistant", "tool"} and ``content`` either ``str`` or
        a list of multimodal parts.

        EverOS: ``{"sender_id" (required), "role", "timestamp" (ms
        epoch, required), "content"}`` with role ∈ {"user",
        "assistant", "tool"} (no ``"system"``).

        Owner mapping (EverOS derives the memory owner from ``sender_id``):
        - ``assistant`` / ``tool`` → ``sender_id = agent_id`` so the
          agent track (cases / skills) accrues under the configured,
          stable agent identity — and ``recall(agent_id=…)`` finds it.
        - ``user`` → keep the caller's ``sender_id`` (the user identity);
          ``recall(user_id=<X>)`` must use that same ``<X>``.

        Other conversions: drop ``system``; missing ``sender_id`` on a
        user message → ``user_id``; ``timestamp`` coerced to ms epoch
        (unparseable → now);
        multimodal ``content`` → space-joined text; empty text → drop.
        """
        now_ms = int(time.time() * 1000)
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", "")).strip()
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
            if not isinstance(content, str):
                content = str(content)
            # An assistant message may carry tool_calls with empty text —
            # keep it (the tool result downstream references its id). The
            # host's tool_calls are already in everos's ToolCallDTO shape
            # (``to_openai_tool_call``); tool messages carry tool_call_id.
            tool_calls = m.get("tool_calls") if role == "assistant" else None
            if not content and not tool_calls:
                continue
            entry: dict[str, Any] = {
                "sender_id": agent_id if role in ("assistant", "tool") else (m.get("sender_id") or user_id),
                "role": role,
                "timestamp": _timestamp_ms(m.get("timestamp"), default_ms=now_ms),
                "content": content,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if role == "tool" and m.get("tool_call_id"):
                entry["tool_call_id"] = m["tool_call_id"]
            out.append(entry)
        return out


def _flatten_profile(profile_data: Any) -> str:
    """Render a profile dict as ``key: value`` lines for prompt
    injection. Non-dicts get ``str()``."""
    if not isinstance(profile_data, dict):
        return str(profile_data)
    return "\n".join(f"{k}: {v}" for k, v in profile_data.items())


# ---------------------------------------------------------------------------
# Factory — entry-point target
# ---------------------------------------------------------------------------


def make_backend(ctx: PluginContext) -> EverosBackend:
    """Plugin entry-point factory. Called by :class:`PluginRegistry`
    after manifest activation. Sync construction only — async setup
    happens in ``EverosBackend.start()``."""
    from raven.config.update_everos import configure_everos_env, ensure_everos_home

    configure_everos_env()
    ensure_everos_home()
    return EverosBackend(ctx)


__all__ = ["EverosBackend", "make_backend"]
