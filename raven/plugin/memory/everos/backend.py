"""EverosBackend — EM-2 embedded mode (with EM-3 HTTP slot reserved).

The backend is the host's :class:`MemoryBackend` implementation. Three
operating modes:

- **embedded** (EM-2, this PR): delegate to ``everos.service`` in
  the same process. If ``everos`` is not installed (or fails to
  import — version skew, missing native deps, etc.), the backend
  degrades to a :class:`_NoOpAdapter` and logs once at construction.
- **http** (EM-3, next PR): HTTP client over EverOS's
  ``POST /api/v1/memory/{search,add,...}``. Currently shadowed by the
  same no-op adapter so wiring code can already select the mode
  without breaking.

Constructor accepts an explicit ``adapter`` so tests can inject a
fake without monkeypatching module-level imports. Production wiring
goes through :func:`make_backend` → ``EverosBackend(ctx)`` →
``_try_make_real_adapter`` which is the only code path that touches
``everos.service``.

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
import time
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import httpx

from raven.memory_engine import Memory
from raven.plugin import PluginContext

logger = logging.getLogger("raven.plugin.memory.everos")

_OwnerType = Literal["user", "agent"]

# Documented operating modes (mirrors ``config_schema.mode`` in the
# plugin manifest). A ``mode`` outside this set is a config typo, not a
# request for a new adapter — see ``EverosBackend._validate_config``.
_VALID_MODES: tuple[str, ...] = ("embedded", "http")

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

    - :class:`_RealEverosAdapter` — lazy-imports ``everos.service``
      and calls in-process.
    - :class:`_NoOpAdapter` — returns ``None`` / swallows writes.
      Used when everos can't be imported, when ``mode != "embedded"``
      until EM-3 lands, and by tests that don't care about everos.
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
    ) -> None: ...


class _NoOpAdapter:
    """Adapter that does nothing. Used as a graceful fallback so callers
    don't need a separate code path for "backend disabled"."""

    async def search(self, **kw: Any) -> Any:
        return None

    async def memorize(self, *a: Any, **kw: Any) -> None:
        return None


class _RealEverosAdapter:
    """In-process delegation to ``everos.service``. The imports happen
    in ``__init__`` so a missing / broken everos fails loudly at
    construction time rather than mysteriously at first ``recall``."""

    def __init__(self) -> None:
        from everos.config import load_settings
        from everos.memory.search.dto import SearchMethod, SearchRequest
        from everos.service.memorize import memorize as _memorize
        from everos.service.search import search as _search

        self._SearchRequest = SearchRequest
        self._SearchMethod = SearchMethod
        self._search_fn = _search
        self._memorize_fn = _memorize
        # Agent-track HYBRID routes skills through everos's cross-encoder
        # lane, which everos refuses (RuntimeError in _validate_components)
        # when no [rerank] provider is configured. Mirror everos's own
        # "configured" test (model + base_url) so we can degrade instead
        # of letting that hard error surface.
        cfg = load_settings().rerank
        self._rerank_configured = bool(cfg.model and cfg.base_url)
        self._degrade_logged = False

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
    ) -> Any:
        # everos 1.0.0's SearchRequest takes user_id XOR agent_id (the
        # owner_id / owner_type pair are read-only derived properties);
        # the backend has already resolved exactly one of these.
        method = self._SearchMethod.HYBRID
        # Agent-track HYBRID needs a rerank provider; when none is
        # configured, degrade to VECTOR (embedding-ranked, single-route,
        # no cross-encoder) so skills still surface rather than erroring.
        # User-track HYBRID never touches the reranker, so it is left as-is.
        if agent_id is not None and not self._rerank_configured:
            method = self._SearchMethod.VECTOR
            if not self._degrade_logged:
                logger.warning(
                    "rerank not configured; agent-track recall degrades "
                    "HYBRID -> VECTOR (no cross-encoder rerank). Configure "
                    "[rerank] (model + base_url) in everos settings to "
                    "enable skill cross-encoder ranking.",
                )
                self._degrade_logged = True
        req = self._SearchRequest(
            user_id=user_id,
            agent_id=agent_id,
            query=query,
            top_k=top_k,
            method=method,
        )
        resp = await self._search_fn(req)
        return resp.data

    async def memorize(
        self,
        session_id: str,
        payload_messages: list[dict[str, Any]],
        *,
        is_final: bool = False,
    ) -> None:
        await self._memorize_fn(
            {"session_id": session_id, "messages": payload_messages},
            is_final=is_final,
        )


def _try_make_real_adapter() -> _Adapter:
    """Return a real adapter if everos imports cleanly; otherwise a
    no-op adapter. Failure is logged at WARNING level so a misconfigured
    deploy is visible without the host crashing."""
    # everos hard-imports the POSIX ``fcntl`` module at import time. On Windows
    # that raised ModuleNotFoundError and the whole memory backend silently
    # degraded to a no-op (mis-logged as "everos not installed"). Install a
    # Windows fcntl shim first so the bundled backend actually loads.
    from raven.utils.win_fcntl_shim import install as _install_fcntl_shim

    _install_fcntl_shim()
    try:
        return _RealEverosAdapter()
    except ModuleNotFoundError as e:
        logger.warning(
            "everos not installed (%s); EverosBackend embedded mode "
            "will degrade to no-op until the package is installed.",
            e,
        )
        return _NoOpAdapter()
    except Exception as e:
        # Distinct from "not installed": the package imported but a
        # symbol/submodule failed to resolve (version skew, rename, etc.).
        # Surfaced louder so a real wiring bug isn't mistaken for an
        # absent optional dependency.
        logger.warning(
            "everos present but failed to initialize (%s); EverosBackend "
            "embedded mode will degrade to no-op. This is likely a "
            "version mismatch or wiring bug, not a missing package.",
            e,
        )
        return _NoOpAdapter()


# ---------------------------------------------------------------------------
# Embedded everos runtime — process-shared, refcounted lifespan
# ---------------------------------------------------------------------------
#
# everos creates its schema (sqlite tables, lancedb indexes) and its OME
# extraction engine in the FastAPI app *lifespan*, not on first service
# call — so embedded mode must drive that lifespan or store()/recall()
# hit "no such table: unprocessed_buffer". everos's engine / stores are
# process-global singletons, so the lifespan is entered once per process
# and shared by every embedded backend, refcounted so the last stop()
# tears it down.

_embedded_lifespan_cm: Any = None
_embedded_lifespan_refs: int = 0


async def _migrate_lancedb_schemas(
    log: logging.Logger,
    *,
    _schemas: Any = None,
    _get_connection: Any = None,
    _get_table: Any = None,
) -> bool:
    """Add missing columns to existing LanceDB tables for forward compatibility.

    Returns True if at least one column was added (caller should retry
    the lifespan), False when nothing needed migration.

    The underscore-prefixed kwargs exist solely for test injection;
    production callers never pass them.
    """
    # Deferred: optional dependency — everos may not be installed.
    import pyarrow as pa

    if _schemas is None:
        from everos.infra.persistence.lancedb import (
            _BUSINESS_SCHEMAS,
            get_connection,
            get_table,
        )

        _schemas = _BUSINESS_SCHEMAS
        _get_connection = get_connection
        _get_table = get_table

    migrated = False
    await _get_connection()
    for schema in _schemas:
        table = await _get_table(schema.TABLE_NAME, schema)
        arrow_schema = await table.schema()
        actual = set(arrow_schema.names)
        expected = set(schema.model_fields.keys())
        missing = expected - actual
        if not missing:
            continue
        fields = [pa.field(col, pa.utf8(), nullable=True) for col in sorted(missing)]
        table.add_columns(pa.schema(fields))
        log.info(
            "EverosBackend: migrated LanceDB table %r — added columns %s",
            schema.TABLE_NAME,
            sorted(missing),
        )
        migrated = True
    return migrated


async def _acquire_embedded_everos(log: logging.Logger) -> None:
    """Enter the shared everos app lifespan (idempotent + refcounted)."""
    global _embedded_lifespan_cm, _embedded_lifespan_refs
    _embedded_lifespan_refs += 1
    if _embedded_lifespan_cm is not None:
        return
    try:
        from everos.entrypoints.api.app import create_app

        app = create_app()
        cm = app.router.lifespan_context(app)
        await cm.__aenter__()
        _embedded_lifespan_cm = cm
        log.info("EverosBackend: embedded everos runtime started")
    except Exception as e:
        # Deferred: optional dependency — everos may not be installed.
        schema_mismatch_cls: type | None = None
        try:
            from everos.infra.persistence.lancedb import LanceDBSchemaMismatchError

            schema_mismatch_cls = LanceDBSchemaMismatchError
        except ImportError:
            pass
        if schema_mismatch_cls is not None and isinstance(e, schema_mismatch_cls):
            log.warning("EverosBackend: LanceDB schema drift detected, attempting auto-migration …")
            try:
                if await _migrate_lancedb_schemas(log):
                    app = create_app()
                    cm = app.router.lifespan_context(app)
                    await cm.__aenter__()
                    _embedded_lifespan_cm = cm
                    log.info("EverosBackend: embedded everos runtime started (after schema migration)")
                    return
            except Exception as retry_err:
                log.warning(
                    "EverosBackend: auto-migration failed (%s); falling back to degraded mode.",
                    retry_err,
                )
        log.warning(
            "EverosBackend: embedded everos init failed (%s); store / recall will degrade until it is available.",
            e,
        )


async def _release_embedded_everos(log: logging.Logger) -> None:
    """Release one ref; tear the lifespan down when the last one drops."""
    global _embedded_lifespan_cm, _embedded_lifespan_refs
    _embedded_lifespan_refs = max(0, _embedded_lifespan_refs - 1)
    if _embedded_lifespan_refs > 0 or _embedded_lifespan_cm is None:
        return
    cm = _embedded_lifespan_cm
    _embedded_lifespan_cm = None
    try:
        await cm.__aexit__(None, None, None)
    except Exception as e:
        log.warning("EverosBackend: embedded everos teardown failed (%s)", e)


# ---------------------------------------------------------------------------
# HTTP adapter — EM-3
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


# Default timeout — HTTP mode is per-turn, so we keep it tight.
_DEFAULT_HTTP_TIMEOUT_S: float = 10.0


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
    ) -> None:
        body = {"session_id": session_id, "messages": payload_messages}
        url = f"{self._base_url}/api/v1/memory/add"
        r = await self._client.post(url, json=body, headers=self._headers())
        r.raise_for_status()
        if is_final:
            # Promote accumulated raw messages to episodes / cases / skills.
            flush_url = f"{self._base_url}/api/v1/memory/flush"
            fr = await self._client.post(
                flush_url,
                json={"session_id": session_id},
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
        self._mode = self._config.get("mode", "embedded")
        self._agent_id: str = self._config.get("agent_id") or _DEFAULT_AGENT_ID
        self._user_id: str = self._config.get("user_id") or _DEFAULT_USER_ID
        # everos accumulates raw turns and only extracts episodes / cases /
        # skills on a boundary flush. Flush every N store() calls so short
        # sessions still build memory (mirrors the EverMe plugin's
        # flush_every_turns=1 default). 0 disables flushing entirely.
        self._flush_every_turns: int = int(
            self._config.get("flush_every_turns", 1),
        )
        self._turn_counts: dict[str, int] = {}
        self._feedback_noop_logged = False
        self._embedded_started = False

        # Adapter selection. Tests inject explicit adapters; production
        # wires through one of the per-mode factories below.
        if adapter is not None:
            self._adapter: _Adapter = adapter
        else:
            self._validate_config()
            if self._mode == "embedded":
                self._adapter = _try_make_real_adapter()
            else:  # "http" — _validate_config rejected anything else
                self._adapter = self._make_http_adapter()

    def _validate_config(self) -> None:
        """Fail fast on a misconfigured plugin config.

        A typo'd ``mode`` (e.g. ``"embeded"``) used to fall through to a
        silent no-op adapter, leaving the agent running with memory
        quietly disabled. Validating the documented enum here surfaces
        the mistake at construction — the registry logs the raised error
        instead of degrading without a trace.
        """
        if self._mode not in _VALID_MODES:
            raise ValueError(
                f"EverosBackend: invalid mode {self._mode!r}; expected one of {', '.join(_VALID_MODES)}",
            )

    def _make_http_adapter(self) -> _Adapter:
        """Construct an :class:`_HttpEverosAdapter` from plugin config.

        Pulls ``base_url`` / ``api_key`` / ``timeout_s`` out of
        ``ctx.config`` with documented defaults. ``base_url`` defaults
        to the EverOS dev port (1995) so a local-dev workflow with the
        server running on the same host needs no extra config.
        """
        base_url = self._config.get("base_url") or "http://localhost:1995"
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
        self._logger.info(
            "EverosBackend.start (mode=%s, adapter=%s)",
            self._mode,
            type(self._adapter).__name__,
        )
        # Embedded real adapter: bring up the in-process everos runtime
        # (schema + OME engine) so store / recall actually work. HTTP and
        # no-op adapters need no local everos lifespan.
        if isinstance(self._adapter, _RealEverosAdapter):
            await _acquire_embedded_everos(self._logger)
            self._embedded_started = True

    async def stop(self) -> None:
        self._logger.info("EverosBackend.stop")
        if self._embedded_started:
            await _release_embedded_everos(self._logger)
            self._embedded_started = False
        # HTTP adapter owns an httpx client when no client was injected;
        # closing it here releases the connection pool. Embedded /
        # no-op adapters expose no aclose so getattr returns None.
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
        n = self._turn_counts.get(session_id, 0) + 1
        self._turn_counts[session_id] = n
        is_final = self._flush_every_turns > 0 and n % self._flush_every_turns == 0
        try:
            await self._adapter.memorize(session_id, payload, is_final=is_final)
        except Exception as e:
            self._logger.warning("EverosBackend.store failed (%s)", e)

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
    # Redirect EverOS to raven's ~/.everos/raven home BEFORE EverosBackend's
    # constructor lazy-imports everos and calls its @cache-d load_settings().
    from raven.config.update_everos import configure_everos_env

    configure_everos_env()
    return EverosBackend(ctx)


__all__ = ["EverosBackend", "_HttpEverosAdapter", "make_backend"]
