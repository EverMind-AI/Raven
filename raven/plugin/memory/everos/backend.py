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

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import httpx

from raven.memory_engine import Memory
from raven.plugin import PluginContext

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
    """

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
    ) -> Any: ...

    async def memorize(
        self,
        session_id: str,
        payload_messages: list[dict[str, Any]],
        *,
        is_final: bool = False,
        app_id: str | None = None,
        project_id: str | None = None,
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


# Default timeout — per-turn, so we keep it tight.
_DEFAULT_HTTP_TIMEOUT_S: float = 360.0


class _HttpEverosAdapter:
    """Adapter that talks to a remote EverOS service over HTTP.

    Endpoints (per the EverOS v1 API brief, see
    ``everos/entrypoints/api/routes/{search,memorize}.py``):

    - ``POST /api/v1/memory/search`` — request body ``SearchRequest``,
      response ``{request_id, data: SearchData}``.
    - ``POST /api/v1/memory/add`` — request body ``MemorizeAddRequest``,
      response ``{request_id, data: AddResponseData}``.

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
        api_key: str | None = None,
        timeout_s: float = _DEFAULT_HTTP_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
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

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
    ) -> Any:
        # Wire contract is user_id XOR agent_id (everos v1 search route).
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if user_id is not None:
            body["user_id"] = user_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        url = f"{self._base_url}/api/v1/memory/search"
        r = await self._client.post(url, json=body, headers=self._headers())
        r.raise_for_status()
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
        app_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "session_id": session_id,
            "messages": payload_messages,
        }
        if app_id is not None:
            body["app_id"] = app_id
        if project_id is not None:
            body["project_id"] = project_id
        url = f"{self._base_url}/api/v1/memory/add"
        r = await self._client.post(url, json=body, headers=self._headers())
        r.raise_for_status()
        if is_final:
            flush_body: dict[str, Any] = {"session_id": session_id}
            if app_id is not None:
                flush_body["app_id"] = app_id
            if project_id is not None:
                flush_body["project_id"] = project_id
            flush_url = f"{self._base_url}/api/v1/memory/flush"
            fr = await self._client.post(
                flush_url,
                json=flush_body,
                headers=self._headers(),
            )
            fr.raise_for_status()


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
        self._flush_every_turns: int = int(
            self._config.get("flush_every_turns", 1),
        )
        self._turn_counts: dict[str, int] = {}
        self._feedback_noop_logged = False

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
        api_key = self._config.get("api_key")
        timeout_s = float(
            self._config.get("timeout_s", _DEFAULT_HTTP_TIMEOUT_S),
        )
        return _HttpEverosAdapter(
            base_url,
            api_key=api_key,
            timeout_s=timeout_s,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        # Build the embedded adapter now (deferred from __init__). The everos /
        # lancedb import is ~2-3s of sync CPU, so run it in a thread to keep it
        # off the event loop.
        if self._mode == "embedded" and self._adapter is None:
            self._adapter = await asyncio.to_thread(_try_make_real_adapter)
        self._logger.info(
            "EverosBackend.start (adapter=%s)",
            type(self._adapter).__name__,
        )
        if isinstance(self._adapter, _HttpEverosAdapter):
            from raven.cli._everos_server import ensure_everos_server

            base_url = self._config.get("base_url") or "http://localhost:18791"
            await ensure_everos_server(base_url)

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
        top_k: int,
    ) -> list[Memory]:
        """Semantic recall via EverOS, scoped to one track.

        ``user_id`` set → everos ``user_id`` → episodes + profiles.
        ``agent_id`` set → everos ``agent_id`` → cases + skills.
        Exactly one must be set (XOR); neither or both → warn + empty.

        Adapter exceptions are caught and logged so a transient EverOS
        failure doesn't cascade into the AgentLoop turn pipeline.
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
        try:
            data = await self._adapter.search(
                user_id=user_id,
                agent_id=agent_id,
                query=query,
                top_k=top_k,
            )
        except Exception as e:
            self._logger.warning(
                "EverosBackend.recall failed (%s); returning empty",
                e,
            )
            return []
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
            app_id=metadata.get("app_id") if metadata else None,
            project_id=metadata.get("project_id") if metadata else None,
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
        user message → ``user_id``; missing ``timestamp`` → now (ms);
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
                "timestamp": m.get("timestamp") or now_ms,
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
