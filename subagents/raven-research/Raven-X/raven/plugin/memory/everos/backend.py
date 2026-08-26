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

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import ssl
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import httpx

from raven.memory_engine import Memory, MemoryApiVersionError
from raven.plugin import PluginContext

logger = logging.getLogger("raven.plugin.memory.everos")

_OwnerType = Literal["user", "agent"]

# Documented operating modes (mirrors ``config_schema.mode`` in the
# plugin manifest). A ``mode`` outside this set is a config typo, not a
# request for a new adapter — see ``EverosBackend._validate_config``.
_VALID_MODES: tuple[str, ...] = ("embedded", "http")

# API prefix selection for HTTP mode. "auto" probes /api/v2 first and
# falls back to /api/v1 (the two prefixes resolve to the same handlers on
# everos >= 1.2.0; 1.1.x only serves v1).
_VALID_API_VERSIONS: tuple[str, ...] = ("auto", "v1", "v2")

# The Cloud's error code for "this account is provisioned for a different
# memory API version". It mounts v2 and answers 403 with this code rather than
# 404, so version negotiation has to recognise it or it pins a prefix the
# account can never use. Observed on api.evermind.ai:
#   {"error": {"code": "VERSION_NOT_ALLOWED", "message": "This account
#    (memory API v1) is not allowed to call the v2 memory API.", ...}}
_VERSION_REFUSED_CODE: str = "VERSION_NOT_ALLOWED"

# everos ``SearchMethod``. Validated locally because ``SearchRequest`` sets
# ``extra="forbid"`` and rejects an unknown value with a 422 that the
# fail-open recall path would swallow -- leaving recall silently empty for the
# whole run rather than naming the typo.
_VALID_RECALL_METHODS: tuple[str, ...] = ("keyword", "vector", "hybrid", "agentic")

_DEFAULT_AGENT_ID: str = "default"
_DEFAULT_USER_ID: str = "default"

# EverOS's own default bind.
_DEFAULT_BASE_URL: str = "http://127.0.0.1:8000"

# Hosts for which cleartext HTTP carries no credential over a network. The
# check is on the host alone: a loopback address never leaves the machine, so
# a bearer token on it is exposed to nothing a local process could not read
# anyway. Anything else -- including a private RFC1918 address -- is a network
# hop, and "internal" is not a synonym for "encrypted".
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})

# Retry only covers failures that prove the request was never acted on; see
# ``_HttpEverosAdapter._post_memory``. First delay, doubling per attempt.
_RETRY_BACKOFF_BASE_S: float = 0.5

# Statuses that mean "refused, nothing done", so a re-send cannot double-apply.
# 429 is the Cloud's over-quota answer. Deliberately not 5xx: a 500 from
# ``/flush`` is the server-side cancel that already committed memcells and
# drained the buffer, which is the one failure a retry must not paper over.
_RETRY_STATUSES: frozenset[int] = frozenset({429})

# Ceiling on an honoured ``Retry-After``. The recall path awaits this inside
# the turn, so an hour-long value published by a rate limiter has to be capped
# into something a turn can survive -- fail-open beats blocking.
_RETRY_AFTER_CAP_S: float = 30.0

# Server-side MemorizeAddRequest caps ``messages`` at 500 per call; a
# larger batch is a 422 that the fail-open store path would swallow, so
# the client chunks instead.
_MAX_MESSAGES_PER_ADD: int = 500

# Per-message content cap, matching everos's own reference client. The
# server has no size limit on ``content``; DR tool results are whole
# extracted pages, so an uncapped capture bloats the unprocessed buffer.
_DEFAULT_CAPTURE_MAX_CHARS: int = 50_000

# Separate, much tighter cap for a folded reasoning block. Measured over 26
# real DR sessions, reasoning runs at 129% of assistant content; sharing the
# content budget would let one turn's deliberation crowd out the conclusions
# of the turns around it in the same /add.
_DEFAULT_REASONING_MAX_CHARS: int = 12_000

# Delimiters for a folded reasoning block. Delimited rather than
# concatenated so the extraction LLM can tell deliberation from assertion:
# unmarked, a rejected hypothesis reads as something the agent concluded.
_REASONING_OPEN = "[reasoning]"
_REASONING_CLOSE = "[/reasoning]"

# everos scope/owner ids become on-disk path segments. This is the
# charset documented in everos's docs/api.md (narrower than the code's,
# deliberately — see the write-path design doc); "." / ".." are the
# server's path-traversal rejects.
_PATH_SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_path_safe(field: str, value: str) -> str:
    """Fail fast on an id the server would 422 — a construction-time
    ValueError beats a mid-batch 422 swallowed by the fail-open store."""
    if value in (".", "..") or not _PATH_SAFE_RE.fullmatch(value):
        raise ValueError(
            f"EverosBackend: {field}={value!r} is not path-safe "
            f"(need ^[A-Za-z0-9_.-]{{1,128}}$, not '.'/'..'); "
            f"everos rejects it with 422"
        )
    return value


_CREDENTIAL_HEADER_RE = re.compile(r"authorization|api[-_]?key|token|secret|bearer", re.I)


def _credential_header_names(headers: dict[str, str]) -> list[str]:
    """Header names that look like they carry a credential.

    Approximate on purpose: a false positive costs one explicit opt-out, a false
    negative puts a secret on the wire.
    """
    return sorted(n for n in headers if _CREDENTIAL_HEADER_RE.search(n))


def _validate_api_key_transport(
    base_url: str,
    api_key: str | None,
    *,
    allow_insecure: bool,
    headers: dict[str, str] | None = None,
) -> None:
    """Refuse to put a credential on an unencrypted network hop.

    ``Authorization: Bearer <token>`` over ``http://`` is the token in
    cleartext to every hop in between, and nothing downstream can tell it was
    exposed -- the request succeeds. So this is a construction-time refusal
    rather than a warning: a warning in a log nobody reads is how a credential
    stays leaked for months, and by the time anyone notices, rotation is the
    only remedy left.

    Exempt: loopback (never on a network) and any ``https`` URL. Escape hatch:
    ``allow_insecure_api_key=true``, for an operator who has weighed a
    plaintext internal endpoint and accepts it. Deliberately verbose to set --
    it should be a decision, not a default.

    Refusing breaks no existing config: a cleartext bearer is exactly the
    combination that was silently unsafe before.
    """
    if allow_insecure:
        return
    # ``headers`` is checked alongside ``api_key`` because an API gateway in
    # front of EverOS is authenticated through a custom header, and a guard that
    # only looked at ``api_key`` was bypassed by the very configuration the
    # header option exists for -- one line of config defeating a
    # construction-time refusal.
    carried = _credential_header_names(headers or {})
    if not api_key and not carried:
        return
    parts = urlsplit(base_url)
    if parts.scheme == "https":
        return
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return
    what = "an api_key" if api_key else "credential header(s) " + ", ".join(carried)
    if api_key and carried:
        what = "an api_key and credential header(s) " + ", ".join(carried)
    raise ValueError(
        f"EverosBackend: refusing to send {what} to {base_url!r} over "
        f"{parts.scheme or 'an unknown scheme'!r} -- a credential on a "
        "non-loopback host without TLS travels in cleartext. Use an https "
        "base_url, or set allow_insecure_api_key=true to accept the exposure "
        "deliberately. If this credential has already been sent this way, treat "
        "it as leaked and rotate it.",
    )


def _sanitize_path_safe(value: str, fallback: str) -> str:
    """Coerce a per-message sender id into the path-safe charset.

    Unlike config ids (validated hard at construction), sender ids
    arrive per message mid-turn; raising here would be swallowed by the
    fail-open store and silently drop the trajectory, so coerce instead.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:128]
    if not cleaned or cleaned in (".", ".."):
        return fallback
    return cleaned


def _exc_label(e: BaseException) -> str:
    """Describe an exception for a fail-open log line.

    The transport errors these paths swallow are mostly timeouts, and
    ``str(httpx.ReadTimeout())`` is the empty string — logging the
    exception alone produces ``failed ()``, which names neither the
    failure nor the budget it blew. Always lead with the type.
    """
    text = str(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class ProbeResult(Enum):
    """Why a ``/health`` probe ended the way it did.

    Ported from upstream raven's service state machine, kept to the four
    outcomes that change what a caller should say. A bare bool collapsed two
    answers that must stay apart: ``REFUSED`` comes back instantly and means
    nothing is listening, while ``TIMEOUT`` means something *is* listening and
    not answering — which charges the full budget every time it is retried.
    """

    OK = "ok"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    ERROR = "error"


def _degradations(health: dict[str, Any]) -> list[str]:
    """What a reachable server says it cannot currently do.

    A 200 from ``/health`` stopped implying a working install at everos 1.2.1,
    which boots on ``[llm]`` alone: a server whose embedding provider is
    misconfigured still answers 200 and quietly degrades recall to keyword
    matching. Only actionable, *current* problems are reported —
    ``failed_permanent`` counts history that no restart will clear, so warning
    on it would fire on every start forever.

    Known blind spot: ``capabilities.embed`` reports whether the provider was
    *configured*, not whether its credentials work. A revoked embedding key
    reads as ``embed: true`` here and surfaces only as cascade retries, which
    is why those are checked too.
    """
    out: list[str] = []
    caps = health.get("capabilities")
    if isinstance(caps, dict):
        if caps.get("embed") is False:
            out.append("embedding unavailable (recall falls back to keyword matching)")
        if caps.get("llm") is False:
            out.append("no LLM configured (extraction cannot run at all)")
    cascade = health.get("cascade")
    if isinstance(cascade, dict):
        if cascade.get("healthy") is False:
            reasons = cascade.get("reasons") or []
            out.append(f"cascade unhealthy ({', '.join(str(r) for r in reasons) or 'no reason given'})")
        retryable = cascade.get("failed_retryable") or 0
        if retryable:
            out.append(f"{retryable} cascade item(s) failing and retrying (often a bad provider credential)")
        streak = cascade.get("drain_consecutive_failures") or 0
        if streak:
            out.append(f"{streak} consecutive cascade drain failures")
    return out


def _fold_reasoning(content: str, raw: Any, max_chars: int) -> str:
    """Prepend an assistant turn's out-of-band reasoning to its text.

    Folded into ``content`` because the server has nowhere else to put it:
    ``MessageItemDTO`` declares no reasoning field and sets no
    ``model_config``, so pydantic's default ``extra="ignore"`` drops a
    sibling key without saying so, and ``ContentItemDTO`` sets
    ``extra="forbid"``, which makes a nested one a 422 for the whole
    request. Reasoning first, matching the order it was generated in.
    """
    if not isinstance(raw, str):
        return content
    reasoning = raw.strip()
    if not reasoning:
        return content
    if max_chars > 0 and len(reasoning) > max_chars:
        reasoning = reasoning[:max_chars]
    block = f"{_REASONING_OPEN}\n{reasoning}\n{_REASONING_CLOSE}"
    return f"{block}\n{content}" if content else block


def _timestamp_ms(raw: Any, fallback: int) -> int:
    """Coerce a host message timestamp to the ms-epoch int the server's
    MessageItemDTO requires.

    AgentLoop stamps ``datetime.now().isoformat()`` strings; passing one
    through gets the whole ``/add`` 422-rejected, which the fail-open
    store swallows — the trajectory silently never reaches EverOS.
    Unparseable values fall back to the caller's synthesized fill-in.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            return fallback
    return fallback


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
        await table.add_columns(pa.schema(fields))
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


# Default timeout — /add and /search are per-turn, so they stay tight.
_DEFAULT_HTTP_TIMEOUT_S: float = 10.0
# ``/health`` is a liveness question asked once at start. Tighter than the
# request budget on purpose: a slow answer means the server is wedged, and
# waiting longer only delays the report it exists to produce.
_HEALTH_TIMEOUT_S: float = 5.0
# /flush runs boundary detection + the user-track extraction LLM inside
# the request, so its budget is separate and much larger.
#
# It must also stay ABOVE the server's own in-request cap
# (``memorize.session_lock_timeout_seconds``). The server cancels its work at
# that cap and answers; a client budget at or below it disconnects first, and
# then the caller cannot tell "the server gave up" from "the server is still
# working". Worse, the server's cancellation is not recoverable by retrying:
# it commits the memcells and drains the buffer before the user-track
# extraction finishes, so a re-flush answers ``no_extraction`` and the
# episodes are gone. Equal budgets are the bad case, not the safe one -- the
# server's timer starts after routing, so it fires first and the client sees
# a 500 instead of an envelope.
#
# 960 clears a server configured at the shipped default (360) or at the 900 we
# run. ``/health`` does not publish the cap, so this is aligned by hand; when
# it does, read it and add a margin instead.
_DEFAULT_FLUSH_TIMEOUT_S: float = 960.0

# After this many consecutive store failures the backend stops
# dispatching for the rest of the process. Store is awaited inside the
# after-turn pipeline, so a dead service otherwise costs every remaining
# turn a full client timeout — on a long DR run that censoring reads as
# a model slowdown, not an infra outage.
_STORE_BREAKER_THRESHOLD: int = 5


class _HttpEverosAdapter:
    """Adapter that talks to a remote EverOS service over HTTP.

    Endpoints (see ``everos/entrypoints/api/routes/{search,memorize}.py``;
    everos >= 1.2.0 mounts the same routers under ``/api/v1`` and
    ``/api/v2``, 1.1.x serves ``/api/v1`` only):

    - ``POST /api/v{1,2}/memory/search`` — request body ``SearchRequest``,
      response ``{request_id, data: SearchData}``.
    - ``POST /api/v{1,2}/memory/add`` — request body ``MemorizeAddRequest``,
      response ``{request_id, data: AddResponseData}``.
    - ``POST /api/v{1,2}/memory/flush`` — promote a session's buffer.

    ``api_version="auto"`` probes v2 on the first request and falls back
    to v1 on 404; the negotiated prefix is cached for the adapter's
    lifetime. ``memorize`` no longer piggybacks a flush on ``is_final``
    — flushing is :meth:`flush`, an explicit separate action owned by
    the backend (deferred-capture profiles flush after the batch / task,
    not per turn).

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
        flush_timeout_s: float = _DEFAULT_FLUSH_TIMEOUT_S,
        api_version: str = "auto",
        app_id: str | None = None,
        project_id: str | None = None,
        defer_extraction: bool = False,
        recall_method: str | None = None,
        enable_llm_rerank: bool = False,
        extra_headers: dict[str, str] | None = None,
        tls_verify: bool | ssl.SSLContext = True,
        retries: int = 0,
        async_mode: bool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})
        self._retries = max(0, int(retries))
        # ``None`` omits the field entirely, which is the only value that is
        # correct on both deployments -- see the config-schema note. Measured on
        # everos 1.2.3: async_mode=false is accepted (no 422) and turns a ~0s
        # /add into a ~33s one, well past the default 10s per-turn budget.
        self._async_mode = async_mode
        self._queued_write_logged = False
        # Kept for the retry cap: a published Retry-After is slept outside
        # httpx's timeout, so it must stay subordinate to the per-request budget.
        self._timeout_s = float(timeout_s)
        self._flush_timeout_s = flush_timeout_s
        self._recall_method = recall_method
        self._enable_llm_rerank = enable_llm_rerank
        # Pinned prefix, or None for "auto" until the first request
        # negotiates one.
        self._api_prefix: str | None = None if api_version == "auto" else api_version
        self._app_id = app_id
        self._project_id = project_id
        self._defer_extraction = defer_extraction
        self._owns_client = client is None
        # ``tls_verify`` defaults to True, which is also httpx's default, so
        # passing it always is a no-op until an operator points it at a private
        # CA bundle (a path) or turns it off.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            verify=tls_verify,
        )

    async def aclose(self) -> None:
        """Close the underlying client if we own it. Idempotent."""
        if self._owns_client:
            await self._client.aclose()

    async def health(self, *, timeout: float = _HEALTH_TIMEOUT_S) -> tuple[ProbeResult, dict[str, Any]]:
        """Ask ``{base_url}/health`` who is there and what they built.

        Unprefixed: ``/health`` sits at the root, outside the versioned
        ``/api/v{1,2}`` mount, so probing it never depends on the prefix
        negotiation having happened yet.

        A malformed body is still ``OK``: the server answered, and the
        capability report is an extra the caller degrades without.

        Retried under the same budget as the write routes, and here a read
        timeout is retryable too: a GET carries no side effect, so the only
        cost of asking twice is time. Worth it because this probe is what
        ``require_service=true`` turns into a hard failure -- against a remote
        endpoint, one connect blip at start-up would otherwise end a run that
        was about to work.
        """
        attempt = 0
        while True:
            try:
                r = await self._client.get(
                    f"{self._base_url}/health",
                    headers=self._headers(),
                    timeout=httpx.Timeout(timeout),
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                probe = (
                    ProbeResult.REFUSED
                    if isinstance(exc, httpx.ConnectError)
                    else ProbeResult.TIMEOUT
                )
                if attempt >= self._retries:
                    return probe, {}
                delay = _RETRY_BACKOFF_BASE_S * (2**attempt)
                attempt += 1
                logger.warning(
                    "everos /health at %s: %s; retry %d/%d in %.1fs",
                    self._base_url,
                    probe.value,
                    attempt,
                    self._retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            except Exception:
                return ProbeResult.ERROR, {}
            if r.status_code in _RETRY_STATUSES and attempt < self._retries:
                # Over quota, not broken. Worth waiting out here specifically
                # because ``require_service=true`` turns this probe into a hard
                # failure, and a rate limit at start-up would otherwise end a
                # run that was about to work.
                published = self._parse_retry_after(r)
                delay = published if published is not None else _RETRY_BACKOFF_BASE_S * (2**attempt)
                attempt += 1
                logger.warning(
                    "everos /health at %s: %d (over quota); retry %d/%d in %.1fs",
                    self._base_url,
                    r.status_code,
                    attempt,
                    self._retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if r.status_code != 200:
                return ProbeResult.ERROR, {}
            try:
                return ProbeResult.OK, r.json() or {}
            except Exception:
                return ProbeResult.OK, {}

    def _headers(self) -> dict[str, str]:
        """Configured headers, with ``Authorization`` applied last.

        Last so a config naming ``Authorization`` cannot shadow the resolved
        ``api_key`` -- that would send a stale token from a file while the env
        var the operator set looked applied. Authenticating through a custom
        header instead is fine and subject to the same TLS check.
        """
        headers = dict(self._extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _scope_fields(self) -> dict[str, str]:
        """app_id / project_id when configured; omitted otherwise so the
        server applies its own defaults (``default``/``default``)."""
        fields: dict[str, str] = {}
        if self._app_id is not None:
            fields["app_id"] = self._app_id
        if self._project_id is not None:
            fields["project_id"] = self._project_id
        return fields

    async def _post_memory(
        self,
        route: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        """POST with a bounded retry, then delegate to the prefix negotiation.

        Retried failures are exactly the ones that prove the request was never
        delivered: ``ConnectError`` (refused / unroutable) and
        ``ConnectTimeout`` (handshake never completed). Nothing reached the
        service, so a re-send cannot double-apply.

        ``429`` joins them, for the same reason rather than as an exception to
        it: the Cloud rate-limiter refuses the request outright, so it did no
        work either. Its ``Retry-After`` is honoured when present -- guessing a
        backoff when the server has published one is how a client turns a rate
        limit into a longer rate limit -- capped at
        ``_RETRY_AFTER_CAP_S`` so a large value cannot park a turn indefinitely.

        Read timeouts and every other error status are NOT retried, and that
        asymmetry is the point. The rule is "was the request provably not
        acted on", not "did it fail": a read timeout on ``/flush`` means the
        server may already have committed the memcells and drained the buffer,
        so re-sending answers ``no_extraction`` and buys nothing, and a 500
        from ``/flush`` is exactly that case observed. Sustained failure is the
        breaker's job, not this loop's.

        ``retries=0`` (the default) makes this a straight pass-through, so a
        local service keeps today's exact behaviour. Note when raising it that
        ``search`` runs inside the turn: its retries are added turn latency,
        whereas ``add`` is dispatched detached and ``flush`` runs at a boundary.
        """
        attempt = 0
        while True:
            try:
                return await self._post_memory_once(route, body, timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt >= self._retries:
                    raise
                delay = _RETRY_BACKOFF_BASE_S * (2**attempt)
                attempt += 1
                logger.warning(
                    "everos %s never reached %s (%s); retry %d/%d in %.1fs",
                    route,
                    self._base_url,
                    type(exc).__name__,
                    attempt,
                    self._retries,
                    delay,
                )
                await asyncio.sleep(delay)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRY_STATUSES or attempt >= self._retries:
                    raise
                published = self._parse_retry_after(exc.response)
                delay = published if published is not None else _RETRY_BACKOFF_BASE_S * (2**attempt)
                attempt += 1
                logger.warning(
                    "everos %s refused with %d (over quota); retry %d/%d in %.1fs%s",
                    route,
                    exc.response.status_code,
                    attempt,
                    self._retries,
                    delay,
                    " (Retry-After)" if published is not None else "",
                )
                await asyncio.sleep(delay)

    def _parse_retry_after(self, response: httpx.Response) -> float | None:
        """``Retry-After`` as seconds, or None when absent / unparseable.

        Both documented forms are accepted -- delta-seconds and an HTTP-date --
        because a server may send either and the date form would otherwise be
        silently read as "no header" and replaced by a guess. A date in the past
        clamps to 0, and everything clamps to ``_RETRY_AFTER_CAP_S``: this delay
        is awaited inside a turn on the recall path.
        """
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            seconds = float(raw)
        except ValueError:
            seconds = None
        if seconds is not None:
            # ``float()`` accepts "nan" and "inf". Clamping handles inf (it
            # caps) but not nan: ``min(max(nan, 0.0), cap)`` is still nan, and
            # ``asyncio.sleep(nan)`` is not a delay anyone chose.
            if not math.isfinite(seconds):
                return None if math.isnan(seconds) else self._cap_retry_after(seconds)
            return self._cap_retry_after(seconds)
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return self._cap_retry_after(delta)

    def _cap_retry_after(self, seconds: float) -> float:
        """Clamp a published delay into something a turn can survive.

        Bounded by ``timeout_s`` too, because this sleep is outside httpx's
        timeout: a 30s ceiling against a 10s request budget would let two
        retries park a recall for 60s.
        """
        return min(max(seconds, 0.0), _RETRY_AFTER_CAP_S, max(self._timeout_s, 1.0))

    @staticmethod
    def _is_version_refusal(response: httpx.Response) -> bool:
        """Is this "your account may not call this API version"?

        Matched on the body's error ``code`` rather than the 403 alone, so an
        ordinary permission failure is not mistaken for a version mismatch.
        Any unparseable body answers False: guessing that a 403 was about
        versioning would turn every auth failure into a second request.
        """
        if response.status_code != 403:
            return False
        try:
            body = response.json()
        except Exception:
            return False
        if not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        return error.get("code") == _VERSION_REFUSED_CODE

    def _raise_if_version_refused(self, response: httpx.Response) -> None:
        """Turn a version refusal into a diagnosis the fail-open paths keep.

        Raised rather than logged-and-swallowed because every caller of this
        adapter fails open: store counts a failure, recall returns ``[]``, and
        the operator is left with "memory was cold" for a run in which no write
        or read was ever possible. The message carries the server's own wording
        plus the one action that resolves it.
        """
        if not self._is_version_refusal(response):
            return
        detail = ""
        try:
            detail = str((response.json().get("error") or {}).get("message") or "")
        except Exception:
            pass
        raise MemoryApiVersionError(
            f"EverOS refused this account's API version at {self._base_url}"
            + (f": {detail}" if detail else "")
            + ". This adapter speaks the v2 memory API (/api/v2/memory/*), which "
            "the docs describe as the current one shared by Cloud and "
            "self-hosted. The legacy Cloud v1 API lives at /api/v1/memories/* "
            "and is a different generation with its own schemas -- not "
            "reachable by changing api_version here. Have the account enabled "
            "for v2 (the migration note says to arrange this with your EverOS "
            "contact); until then every store and recall on this backend will "
            "fail open and the agent will run with no memory.",
        )

    async def _post_memory_once(
        self,
        route: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        """POST to ``/api/<prefix>/memory/<route>``, negotiating the prefix.

        With ``api_version="auto"`` the first request tries v2 and falls back to
        v1 on ``404`` -- the prefix is not mounted, which is the self-hosted
        1.1.x case.

        ``403 VERSION_NOT_ALLOWED`` is NOT a fallback trigger, and that is a
        deliberate reversal of the obvious reading. It means the Cloud mounts v2
        but this account is provisioned for the legacy v1 API, whose Cloud paths
        are ``/api/v1/memories/*`` -- plural, a different generation with its own
        schemas, not the ``/api/v1/memory/*`` this adapter speaks for
        self-hosted. Retrying v1 therefore replaces a precise, documented
        diagnosis with a bare 404 from a path that cannot exist. Measured
        against api.evermind.ai: probing unauthenticated, ``/api/v1/memories/*``
        answers 401 (present) while ``/api/v1/memory/*`` answers 404 (absent).

        The refusal is raised as :class:`MemoryApiVersionError` so the reason
        survives the fail-open call sites, which would otherwise reduce it to
        "recall returned nothing".
        """
        kwargs: dict[str, Any] = {"json": body, "headers": self._headers()}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        if self._api_prefix is not None:
            r = await self._client.post(
                f"{self._base_url}/api/{self._api_prefix}/memory/{route}",
                **kwargs,
            )
            self._raise_if_version_refused(r)
            r.raise_for_status()
            return r
        r = await self._client.post(
            f"{self._base_url}/api/v2/memory/{route}",
            **kwargs,
        )
        self._raise_if_version_refused(r)
        if r.status_code != 404:
            # Not on 429: a gateway can throttle before it routes, so the
            # status is no evidence that this prefix is the served one.
            # Pinning on it would survive the retry and outlive the throttle.
            if r.status_code not in _RETRY_STATUSES:
                self._api_prefix = "v2"
            r.raise_for_status()
            return r
        r = await self._client.post(
            f"{self._base_url}/api/v1/memory/{route}",
            **kwargs,
        )
        # Checked here too: a deployment that refuses v1 by account version
        # would otherwise surface as a bare 404/403 on the last arm, which is
        # the diagnosis this whole branch exists to preserve.
        self._raise_if_version_refused(r)
        if r.status_code != 404:
            self._api_prefix = "v1"
        r.raise_for_status()
        return r

    async def search(
        self,
        *,
        user_id: str | None,
        agent_id: str | None,
        query: str,
        top_k: int,
    ) -> Any:
        # Wire contract is user_id XOR agent_id. Scope keys ride along
        # when configured — a store scoped to app/project is only
        # recallable from that same scope (hard isolation server-side).
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if user_id is not None:
            body["user_id"] = user_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        # Both are real ``SearchRequest`` fields, and only sent when they
        # differ from the server's own default: the model sets
        # ``extra="forbid"``, so anything the running build does not know
        # turns a working search into a 422 rather than being ignored.
        if self._recall_method is not None:
            body["method"] = self._recall_method
        if self._enable_llm_rerank:
            # Server-side: applies to agent_case / agent_skill fusion only
            # (the episode path has its own fact eviction and ignores it), and
            # it costs an LLM call inside the search request -- i.e. inside
            # the turn.
            body["enable_llm_rerank"] = True
        body.update(self._scope_fields())
        r = await self._post_memory("search", body)
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
        # ``is_final`` is part of the shared adapter protocol (the
        # embedded adapter forwards it into everos.service.memorize);
        # over HTTP flushing is decoupled — see :meth:`flush`.
        body: dict[str, Any] = {
            "session_id": session_id,
            "messages": payload_messages,
        }
        body.update(self._scope_fields())
        if self._defer_extraction:
            # Deferred capture: /add only merges into the persistent
            # unprocessed buffer — no boundary LLM, no provider deps.
            # Servers without the field (<= 1.2.3 tags) ignore it and
            # run eager; version strings can't tell the two apart, so
            # scripts/everos_preflight.py probes by behavior (timing a
            # deferred add) and deployments pin the service by sha.
            body["defer_extraction"] = True
        if self._async_mode is not None:
            # Sent only when set, because the right value is opposite on the
            # two deployments and neither default is safe for the other. See
            # ``_async_mode`` in the backend for the measurements.
            body["async_mode"] = self._async_mode
        r = await self._post_memory("add", body)
        if r.status_code == 202 and not self._queued_write_logged:
            # 202 means the server queued the messages rather than applying
            # them. ``raise_for_status`` is happy, so nothing else would say
            # so -- and the host's pre-flush barrier
            # (``AgentLoop.drain_backend_stores``) can only guarantee that this
            # HTTP call returned, not that the server acted on it. A flush that
            # overtakes its own writes promotes a partial trajectory.
            self._queued_write_logged = True
            logger.warning(
                "everos accepted /add with 202 (queued, not applied). A flush "
                "for this session may run before these messages are buffered "
                "and promote a partial trajectory; set async_mode=false to "
                "make the write synchronous, or promote out of band once the "
                "queue has drained.",
            )
        return None

    async def probe_version(self, *, user_id: str) -> None:
        """Smallest authenticated call that can trip a version refusal.

        Not :meth:`search`: that carries the configured ``method`` and
        ``enable_llm_rerank``, so probing through it could fire a server-side
        LLM call at start-up. Only the status is read.
        """
        body: dict[str, Any] = {"query": "", "top_k": 1, "user_id": user_id}
        body.update(self._scope_fields())
        await self._post_memory("search", body, timeout=_HEALTH_TIMEOUT_S)

    async def flush(self, session_id: str) -> None:
        """Promote a session's buffered messages to episodes / cases /
        skills. Scope keys must match the ones sent on ``/add`` — the
        server resolves the buffer by (session, app, project)."""
        body: dict[str, Any] = {"session_id": session_id}
        body.update(self._scope_fields())
        await self._post_memory("flush", body, timeout=self._flush_timeout_s)


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
        # ``defer_extraction=true`` overrides this cadence: capture is
        # buffer-only and flushing becomes an explicit action (the
        # :meth:`flush` method / the post-batch flush script).
        self._flush_every_turns: int = int(
            self._config.get("flush_every_turns", 1),
        )
        self._defer_extraction: bool = bool(
            self._config.get("defer_extraction", False),
        )
        # Hard-isolation scope for every write (and, when recall stays
        # on, every search). ``None`` omits the field — the server
        # scopes to ``default``/``default``.
        self._app_id: str | None = self._config.get("app_id") or None
        self._project_id: str | None = self._config.get("project_id") or None
        self._capture_max_chars: int = int(
            self._config.get("capture_max_chars", _DEFAULT_CAPTURE_MAX_CHARS),
        )
        self._emit_user_messages: bool = bool(
            self._config.get("emit_user_messages", True),
        )
        # Fold an assistant turn's out-of-band reasoning into the text sent
        # for capture. Off by default: it roughly doubles the assistant bytes
        # a session writes, and what the extractors make of deliberation
        # (versus assertion) is not yet measured.
        self._capture_reasoning: bool = bool(
            self._config.get("capture_reasoning", False),
        )
        self._capture_reasoning_max_chars: int = int(
            self._config.get("capture_reasoning_max_chars", _DEFAULT_REASONING_MAX_CHARS),
        )
        # Turn the start-time probe from a warning into a hard failure. Off by
        # default (an interactive session must not lose its answer because
        # memory is down); on for a scripted run whose whole purpose is to
        # write memory, where an hour spent writing into nothing is the worse
        # outcome. See :meth:`_check_service`.
        self._require_service: bool = bool(
            self._config.get("require_service", False),
        )
        self._service_checked = False
        self._warm_task: asyncio.Task | None = None
        # ``None`` omits the field so the server applies its own default
        # (HYBRID). Declared in the manifest since EM-2 but never sent until
        # now, so ``recall_method`` silently did nothing.
        self._recall_method: str | None = self._config.get("recall_method") or None
        self._enable_llm_rerank: bool = bool(
            self._config.get("enable_llm_rerank", False),
        )
        # D8 store-only gate, read by the host at wiring time: False
        # keeps the backend on the after-turn store dispatch only — the
        # context engine never sees it, so neither the ``# Memory``
        # recall lane nor the Everos skill source is assembled.
        self.recall_enabled: bool = bool(
            self._config.get("recall_enabled", True),
        )
        # Per-lane refinement *inside* ``recall_enabled``. The master switch
        # stays the one that makes prompt bytes identical to memory-off (what
        # lets a capture profile ride a measurement arm); these two pick which
        # lane is worth its cost once recall is on at all. A research agent
        # typically wants skills without episodes: the distilled method
        # transfers between questions, the episode summary of question N-1
        # does not.
        self.recall_memory_enabled: bool = bool(
            self._config.get("recall_memory_enabled", True),
        )
        self.recall_skills_enabled: bool = bool(
            self._config.get("recall_skills_enabled", True),
        )
        # When set, everos session ids are generated locally as
        # ``<prefix>_<uuid4hex>`` per host session key instead of reusing
        # the host's key verbatim. Sharing one everos session id across
        # writers merges their unprocessed buffers and cross-copies agent
        # cases (server-side fan-out), so writers own their session ids.
        self._session_id_prefix: str | None = self._config.get("session_id_prefix") or None
        self._session_ids: dict[str, str] = {}
        self._recorded_sessions: set[str] = set()
        self._turn_counts: dict[str, int] = {}
        self._last_ts: dict[str, int] = {}
        self._store_failures = 0
        # Counted apart from store failures because the two lose different
        # things. A failed store means the turn never reached the service, so
        # nothing about it is indexed. A failed flush means the turns are
        # already durable in the service's buffer and only promotion did not
        # happen -- recoverable out of band. Reporting both in store's language
        # sends the reader looking for a write that did happen.
        self._flush_failures = 0
        # Recall fails open by returning an empty list, which is
        # indistinguishable from "nothing matched" at every call site. Without
        # a counter, a run whose every recall was refused looks exactly like a
        # run with a cold memory -- measured against the Cloud, where a
        # version mismatch made all three lanes fail while the turns still
        # completed and the agent read no memory at all.
        self._recall_failures = 0
        # One ERROR per process for a version refusal. The condition cannot
        # change while the process runs, so per-call reporting only buries it.
        self._version_refusal_logged = False
        self._consecutive_store_failures = 0
        self._store_breaker_open = False
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
        instead of degrading without a trace. Scope / owner ids get the
        same treatment: server-side they are 422s that the fail-open
        store path would swallow mid-batch.
        """
        if self._mode not in _VALID_MODES:
            raise ValueError(
                f"EverosBackend: invalid mode {self._mode!r}; expected one of {', '.join(_VALID_MODES)}",
            )
        api_version = self._config.get("api_version", "auto")
        if api_version not in _VALID_API_VERSIONS:
            raise ValueError(
                f"EverosBackend: invalid api_version {api_version!r}; expected one of {', '.join(_VALID_API_VERSIONS)}",
            )
        if self._recall_method is not None and self._recall_method not in _VALID_RECALL_METHODS:
            raise ValueError(
                f"EverosBackend: invalid recall_method {self._recall_method!r}; "
                f"expected one of {', '.join(_VALID_RECALL_METHODS)}",
            )
        if self._defer_extraction and self._mode == "embedded":
            raise ValueError(
                "EverosBackend: defer_extraction=true requires mode='http' — "
                "the embedded adapter exposes no flush, so a deferred buffer "
                "could never be promoted",
            )
        if self._mode == "http":
            retries = self._config.get("retries", 0)
            if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
                raise ValueError(
                    f"EverosBackend: retries must be a non-negative integer, got {retries!r}",
                )
            _validate_api_key_transport(
                self._resolve_base_url(),
                self._resolve_api_key(),
                allow_insecure=bool(self._config.get("allow_insecure_api_key", False)),
                headers=self._resolve_extra_headers(),
            )
        for field, value in (
            ("app_id", self._app_id),
            ("project_id", self._project_id),
            ("agent_id", self._agent_id),
        ):
            if value is not None:
                _validate_path_safe(field, value)

    def _resolve_base_url(self) -> str:
        """Config, then ``EVEROS_BASE_URL``, then the local default.

        Symmetric with :meth:`_resolve_api_key`, else pointing a run at a remote
        service means editing a committed file. Shared with ``_validate_config``
        so the value checked is the value used.
        """
        return self._config.get("base_url") or os.environ.get("EVEROS_BASE_URL") or _DEFAULT_BASE_URL

    def _resolve_api_key(self) -> str | None:
        # Environment first-class, matching the web tools: a bearer token in a
        # config file is a secret in a file people share and commit, and the
        # config had been the only way to supply one.
        return self._config.get("api_key") or os.environ.get("EVEROS_API_KEY") or None

    def _resolve_tls_verify(self) -> bool | ssl.SSLContext:
        """``True`` (verify), ``False`` (do not), or a CA bundle as a context.

        A path is turned into an :class:`ssl.SSLContext` here rather than handed
        to httpx as a string: ``verify=<str>`` is deprecated in httpx 0.28 and
        slated for removal, so passing the path through would work now and
        break on an upgrade.

        Building the context here also moves an unreadable or malformed bundle
        to construction time, named -- httpx would raise a bare ``SSLError``
        mentioning neither the file nor which setting produced it.

        ``False`` is accepted because refusing it outright pushes people to
        drop TLS altogether, but it warns every boot: it leaves the channel
        encrypted while authenticating nobody, which is a quieter failure than
        plain http rather than a smaller one.
        """
        value = self._config.get("tls_verify", True)
        if isinstance(value, bool):
            if not value:
                logger.warning(
                    "EverosBackend: tls_verify=false -- the server's "
                    "certificate is not checked, so an intercepting proxy is "
                    "indistinguishable from the real service. Prefer a CA "
                    "bundle path.",
                )
            return value
        path = str(value)
        try:
            return ssl.create_default_context(cafile=path)
        except (OSError, ssl.SSLError) as exc:
            raise ValueError(
                f"EverosBackend: tls_verify={path!r} is not a usable CA bundle ({exc})",
            ) from exc

    def _make_http_adapter(self) -> _Adapter:
        """Construct an :class:`_HttpEverosAdapter` from plugin config.

        Pulls ``base_url`` / ``api_key`` / ``timeout_s`` /
        ``flush_timeout_s`` / ``api_version``, the remote-transport
        options and the scope keys out of ``ctx.config`` with documented
        defaults. ``base_url`` defaults to EverOS's own default bind
        (127.0.0.1:8000).
        """
        base_url = self._resolve_base_url()
        api_key = self._resolve_api_key()
        timeout_s = float(
            self._config.get("timeout_s", _DEFAULT_HTTP_TIMEOUT_S),
        )
        flush_timeout_s = float(
            self._config.get("flush_timeout_s", _DEFAULT_FLUSH_TIMEOUT_S),
        )
        return _HttpEverosAdapter(
            base_url,
            api_key=api_key,
            timeout_s=timeout_s,
            flush_timeout_s=flush_timeout_s,
            api_version=self._config.get("api_version", "auto"),
            app_id=self._app_id,
            project_id=self._project_id,
            defer_extraction=self._defer_extraction,
            recall_method=self._recall_method,
            enable_llm_rerank=self._enable_llm_rerank,
            extra_headers=self._resolve_extra_headers(),
            tls_verify=self._resolve_tls_verify(),
            async_mode=self._resolve_async_mode(),
            retries=int(self._config.get("retries", 0)),
        )

    def _resolve_async_mode(self) -> bool | None:
        """``add``'s synchronous/asynchronous choice, or None to omit it.

        Omitted by default because the safe value is opposite on the two
        deployments: Cloud answers ``202 queued``, and nothing downstream tells
        a queued write from an applied one, so a flush can overtake its own
        messages (``false`` fixes that); self-hosted already writes in-request,
        and ``false`` there costs ~33s per add on everos 1.2.3 against the 10s
        default budget. An unset value is therefore not "false".
        """
        value = self._config.get("async_mode")
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(
                f"EverosBackend: async_mode must be a boolean, got {value!r}",
            )
        return value

    def _resolve_extra_headers(self) -> dict[str, str]:
        """``headers`` from config, coerced to str/str.

        Values are stringified rather than rejected: a port or a numeric tenant
        id written unquoted in JSON arrives as an int, and httpx raises on a
        non-str header value -- which would surface as a crash on the first
        request rather than at construction.
        """
        raw = self._config.get("headers") or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"EverosBackend: headers must be a mapping of name to value, got {type(raw).__name__}",
            )
        return {str(k): str(v) for k, v in raw.items()}

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
        await self._check_service()
        await self._warm_recall()

    async def _warm_recall(self) -> None:
        """Pay the agent-lane cold start before a turn has to.

        The first agent-track search on a fresh process opens the LanceDB
        tables and loads the embedding model; measured warm it is ~1.9s, cold
        it can exceed the 10s per-request budget. Paid inside a turn that cost
        is a timeout, the recall returns empty, and the turn silently runs
        without the skill it should have had.

        Only when skill recall is actually on, and detached with no wait: this
        runs on the loop every session starts on, and a warm-up that blocks
        start would just move the same cost somewhere more visible. Failures
        are ignored -- ``_check_service`` has already reported anything real,
        and a warm-up is by definition best-effort.
        """
        if not (self.recall_enabled and self.recall_skills_enabled):
            return
        if not isinstance(self._adapter, (_HttpEverosAdapter, _RealEverosAdapter)):
            return

        async def _warm() -> None:
            try:
                await self._adapter.search(
                    user_id=None,
                    agent_id=self._agent_id,
                    query="warmup",
                    top_k=1,
                )
            except Exception as e:
                self._logger.debug("EverosBackend: recall warm-up failed (%s)", _exc_label(e))

        try:
            self._warm_task = asyncio.get_running_loop().create_task(_warm())
        except RuntimeError:  # no running loop (sync context)
            self._warm_task = None

    async def _check_api_version(self) -> None:
        """Ask, before the first turn, whether this account may use this API.

        ``/health`` is not enough on the Cloud: it is unauthenticated there
        (measured -- a wrong key still gets ``{"message": "ok"}``), so a healthy
        probe says only that the gateway is up. An account provisioned for the
        legacy v1 API therefore starts clean and then fails open on every single
        store and recall, which is precisely the "spend an hour writing into
        nothing" outcome ``require_service`` exists to prevent.

        Kept genuinely cheap, which the obvious implementation is not: routing
        through the configured ``recall_method`` / ``enable_llm_rerank`` would
        make a probe with ``method="agentic"`` plus rerank fire a synchronous
        LLM call inside ``start()`` and burn the whole request budget. The probe
        asks the one question it is for, with the cheapest retrieval mode and a
        short deadline of its own.

        Any failure other than a version refusal is left to the paths that
        already own it -- a probe must not be stricter than the operation it
        probes.
        """
        probe = getattr(self._adapter, "probe_version", None)
        if probe is None or not self._resolve_api_key():
            # No credential means no account, hence no per-account version
            # provisioning to hit. The self-hosted default path stays a single
            # /health request, as before.
            return
        try:
            await probe(user_id=self._user_id)
        except MemoryApiVersionError as e:
            if self._require_service:
                raise
            self._report_version_refusal(e)
        except Exception:
            # Not this check's business. A 422 on an empty query, a transient
            # 5xx, a timeout -- all are reported by the call sites that own
            # them, and failing start() on them would make a probe stricter
            # than the operation it is probing.
            pass

    async def _check_service(self) -> None:
        """Probe the service once, and say what it cannot do.

        Ported from upstream raven, minus the spawner: this deployment
        connects to an EverOS the operator runs, and starting one would take
        the OME jobstore lock that is theirs to grant. Upstream refuses to
        spawn for exactly this case too (its ``everos_owned()`` branch).

        Without this, an absent service first surfaces as a store failure
        several turns in, and a *degraded* one never surfaces at all: a batch
        can run to completion writing into a pipeline that drops everything.
        The probe converts both into one line before the first turn.

        Never fatal unless ``require_service`` is set. The default is
        fail-open because a turn is already durable in the session log by the
        time the backend sees it, so an absent memory service must not cost
        the user their answer.
        """
        health = getattr(self._adapter, "health", None)
        if health is None:
            # Embedded / no-op adapters expose no /health. Said out loud when
            # the operator asked for the guarantee, because "scripted capture
            # that must not write into nothing" is exactly the case that
            # reaches for require_service, and getting zero protection from it
            # silently is worse than not having the switch.
            if self._require_service:
                self._logger.warning(
                    "EverosBackend: require_service=true has no effect in "
                    "mode=%s -- only the HTTP adapter can probe a service. "
                    "The run continues unprotected.",
                    self._mode,
                )
            return
        result, payload = await health()
        if result is not ProbeResult.OK:
            base_url = self._resolve_base_url()
            reason = {
                ProbeResult.REFUSED: "nothing is listening",
                ProbeResult.TIMEOUT: "it is listening but not answering",
                ProbeResult.ERROR: "it answered, but not with a healthy /health",
            }[result]
            message = (
                f"EverOS at {base_url} is not usable ({result.value}: {reason}); "
                "captured turns will not be indexed"
            )
            if self._require_service:
                from raven.memory_engine.backend import MemoryServiceUnavailableError

                raise MemoryServiceUnavailableError(message)
            self._logger.warning(
                "%s. Set require_service=true to fail the run instead of degrading.",
                message,
            )
            return
        self._service_checked = True
        await self._check_api_version()
        degradations = _degradations(payload)
        if not degradations:
            self._logger.info(
                "EverosBackend: everos %s is healthy",
                payload.get("version", "?"),
            )
            return
        # A degraded service is the harder fault to attribute -- it looks like
        # an agent that merely learned nothing -- so it is a warning even
        # though every request will succeed.
        self._logger.warning(
            "EverosBackend: everos %s is reachable but degraded: %s",
            payload.get("version", "?"),
            "; ".join(degradations),
        )
        if self._require_service:
            from raven.memory_engine.backend import MemoryServiceUnavailableError

            raise MemoryServiceUnavailableError(
                f"EverOS is reachable but degraded: {'; '.join(degradations)}",
            )

    async def stop(self) -> None:
        self._logger.info("EverosBackend.stop")
        if self._warm_task is not None and not self._warm_task.done():
            # Cancelled because it is a warm-up: its only value was to finish
            # before a turn needed it, and stop() means none will. Awaited
            # anyway so the cancellation is collected here rather than
            # surfacing as "Task was destroyed but it is pending" once the loop
            # closes.
            self._warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warm_task
        # Store, flush and recall are all fail-open per call; the aggregates are
        # surfaced once at teardown so silent loss is visible in the session log.
        if self._recall_failures:
            self._logger.warning(
                "EverosBackend: %d recall(s) failed this session (fail-open; "
                "they returned empty, which every call site reads as 'nothing "
                "matched' -- the agent ran without the memory it asked for)",
                self._recall_failures,
            )
        if self._store_failures:
            self._logger.warning(
                "EverosBackend: %d store dispatches failed this session "
                "(fail-open; those turns never reached the service and are "
                "not indexed)",
                self._store_failures,
            )
        if self._flush_failures:
            self._logger.warning(
                "EverosBackend: %d flush dispatches failed this session "
                "(fail-open; the captured turns are buffered service-side, "
                "but derived memory was not promoted -- retry out of band "
                "with scripts/everos_flush_batch.py). A flush that failed on "
                "the server's own in-request timeout may have committed part "
                "of its work already, in which case a retry answers "
                "no_extraction and the rest is not recoverable",
                self._flush_failures,
            )
        if self._store_breaker_open:
            self._logger.warning(
                "EverosBackend: store breaker was open at teardown — "
                "stores after it opened were suppressed and are not in "
                "the failure count",
            )
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
            # Counted like any other fail-open empty: the caller cannot tell
            # this apart from "nothing matched" either.
            self._recall_failures += 1
            return []
        owner_type: _OwnerType = "user" if user_id is not None else "agent"
        try:
            data = await self._adapter.search(
                user_id=user_id,
                agent_id=agent_id,
                query=query,
                top_k=top_k,
            )
        except MemoryApiVersionError as e:
            # Still fails open, and deliberately so: this repo's rule is that
            # an interactive session degrades rather than losing its answer,
            # and ``require_service`` is the existing opt-in for turning a
            # dead memory service into a hard failure -- see the probe in
            # ``start()``, which is where this condition is escalated. What
            # changes here is only the volume: ERROR once per process, because
            # a permanent misconfiguration reported at WARNING once per turn
            # reads as noise and gets skimmed past.
            self._recall_failures += 1
            self._report_version_refusal(e)
            return []
        except Exception as e:
            self._recall_failures += 1
            self._logger.warning(
                "EverosBackend.recall failed on the %s lane (%s); returning empty",
                owner_type,
                _exc_label(e),
            )
            return []
        if data is None:
            return []
        return self._search_data_to_memories(data, owner_type)

    def _report_version_refusal(self, exc: MemoryApiVersionError) -> None:
        """Log a version refusal at ERROR, once for the process."""
        if self._version_refusal_logged:
            return
        self._version_refusal_logged = True
        self._logger.error("%s", exc)

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

        Fail-open with a breaker: after ``_STORE_BREAKER_THRESHOLD``
        consecutive failures store becomes a no-op for the rest of the
        process, so a dead service stops charging every turn a full
        client timeout. A success resets the streak.
        """
        if self._store_breaker_open:
            return
        if not messages:
            return
        payload = self._convert_messages(
            messages,
            agent_id=self._agent_id,
            user_id=self._user_id,
            capture_max_chars=self._capture_max_chars,
            emit_user_messages=self._emit_user_messages,
            capture_reasoning=self._capture_reasoning,
            capture_reasoning_max_chars=self._capture_reasoning_max_chars,
        )
        if not payload:
            return
        everos_session = self._everos_session_id(session_id)
        self._record_deferred_session(everos_session)
        # Continue the strictly-increasing timestamp line across store()
        # calls: two same-ms fallback fills in consecutive turns would
        # reuse a (session, ts_ms, index) dedup key and be silently
        # dropped server-side.
        floor = self._last_ts.get(everos_session)
        for m in payload:
            if floor is not None and m["timestamp"] <= floor:
                m["timestamp"] = floor + 1
            floor = m["timestamp"]
        self._last_ts[everos_session] = floor
        n = self._turn_counts.get(session_id, 0) + 1
        self._turn_counts[session_id] = n
        # Deferred capture never flushes from store(): promotion is an
        # explicit action (flush() / the post-batch flush script), so
        # no extraction LLM runs inside the turn.
        is_final = (
            not self._defer_extraction
            and self._flush_every_turns > 0
            and n % self._flush_every_turns == 0
        )
        chunks = [
            payload[i : i + _MAX_MESSAGES_PER_ADD]
            for i in range(0, len(payload), _MAX_MESSAGES_PER_ADD)
        ]
        try:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                await self._adapter.memorize(
                    everos_session,
                    chunk,
                    is_final=is_final and last,
                )
            if is_final:
                # HTTP adapters flush explicitly (add/flush decoupled);
                # the embedded adapter flushes via ``is_final`` above and
                # exposes no ``flush``.
                await self._adapter_flush(everos_session)
            self._consecutive_store_failures = 0
        except Exception as e:
            self._store_failures += 1
            self._consecutive_store_failures += 1
            if isinstance(e, MemoryApiVersionError):
                # The store-only profile (``recall_enabled=false``) never runs
                # a recall, so without this the one deployment shape that is
                # write-only would be the one that never sees the diagnosis --
                # just a per-turn warning until the breaker opens.
                self._report_version_refusal(e)
            self._logger.warning("EverosBackend.store failed (%s)", _exc_label(e))
            if (
                not self._store_breaker_open
                and self._consecutive_store_failures >= _STORE_BREAKER_THRESHOLD
            ):
                self._store_breaker_open = True
                self._logger.warning(
                    "EverosBackend: %d consecutive store failures; "
                    "suppressing further EverOS writes for this process "
                    "(service likely down)",
                    self._consecutive_store_failures,
                )

    async def flush(self, session_id: str) -> None:
        """Explicitly promote a session's buffered messages.

        The deferred-capture profile's flush trigger: call at a natural
        boundary (task completion) — measurement batches flush out of
        band via ``scripts/everos_flush_batch.py`` instead. ``session_id``
        is the host's key; the generated everos session id (if any) is
        resolved through the same map ``store`` used — including the
        on-disk half of it, so a later process can promote what an
        earlier one buffered. Fail-open like ``store`` — a flush failure
        must not break the host's turn.

        Two early returns, both about not spending a 360s budget on a
        request that cannot succeed: a session id this deployment has
        never written under (promoting it would ask the service about a
        buffer it has never seen), and an open store breaker (every
        write of this session was suppressed, so there is nothing
        buffered to promote).
        """
        everos_session = self._resolve_known_session_id(session_id)
        if everos_session is None:
            self._logger.debug(
                "EverosBackend.flush: no everos session recorded for %s; nothing buffered",
                session_id,
            )
            return
        if self._store_breaker_open:
            self._logger.warning(
                "EverosBackend.flush: store breaker is open, so nothing was "
                "buffered for %s; skipping promotion",
                session_id,
            )
            return
        flush = getattr(self._adapter, "flush", None)
        if flush is None:
            self._logger.warning(
                "EverosBackend.flush: adapter %s exposes no flush; skipped",
                type(self._adapter).__name__,
            )
            return
        try:
            await flush(everos_session)
        except Exception as e:
            # Deliberately not ``_consecutive_store_failures``: a flush is one
            # request per task boundary, not per turn, so it must not trip the
            # store breaker and suppress the writes of a healthy service.
            self._flush_failures += 1
            if isinstance(e, MemoryApiVersionError):
                self._report_version_refusal(e)
            self._logger.warning("EverosBackend.flush failed (%s)", _exc_label(e))

    async def _adapter_flush(self, everos_session: str) -> None:
        flush = getattr(self._adapter, "flush", None)
        if flush is not None:
            await flush(everos_session)

    def _everos_session_id(self, session_id: str) -> str:
        """Resolve the host session key to the everos session id.

        Without a configured prefix the host key passes through
        verbatim (legacy behavior). With one, each host session gets a
        locally generated ``<prefix>_<uuid4hex>`` — everos merges
        same-session buffers across writers and fans agent cases out
        across every assistant sender in a cell, so a session id must
        never be shared between writers.

        The mapping is persisted per workspace, so the same host session
        key resolves to the same everos session id in a later process.
        Without that, ``raven agent -m ... -s one_session`` run N times
        wrote N unmergeable single-turn buffers — the host says those
        turns are one conversation and the session log keeps them as one,
        so the capture side has to agree.
        """
        if self._session_id_prefix is None:
            return session_id
        sid = self._resolve_known_session_id(session_id)
        if sid is None:
            sid = self._persist_session_id(
                session_id,
                f"{self._session_id_prefix}_{uuid.uuid4().hex}",
            )
            self._session_ids[session_id] = sid
            self._logger.info(
                "EverosBackend: session %s writes as everos session %s",
                session_id,
                sid,
            )
        return sid

    def _resolve_known_session_id(self, session_id: str) -> str | None:
        """The everos session id for a host key, or ``None`` if there is none.

        ``None`` only ever means "no id was ever minted for this host key
        under this scope", which is why the answer is trustworthy enough
        for ``flush`` to skip on. With no prefix configured the host key
        *is* the everos session id and a buffer under it may have been
        written by a process that left no local trace, so there is
        nothing that could prove it absent — hence the passthrough.
        """
        if self._session_id_prefix is None:
            return session_id
        sid = self._session_ids.get(session_id)
        if sid is None:
            sid = self._read_session_map().get(self._session_map_key(session_id))
            if sid is not None:
                self._session_ids[session_id] = sid
        return sid

    def _session_map_key(self, session_id: str) -> str:
        """Scope-qualified map key.

        ``(app_id, project_id)`` is hard isolation server-side, so the same
        host session key under a different scope is a different session and
        must not inherit the other's id.
        """
        return f"{self._app_id or 'default'}/{self._project_id or 'default'}/{session_id}"

    def _session_map_path(self) -> Path | None:
        workspace = getattr(self._services, "workspace", None)
        if workspace is None:
            return None
        return Path(workspace) / ".everos_session_map.json"

    def _read_session_map(self) -> dict[str, str]:
        path = self._session_map_path()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            self._logger.warning(
                "EverosBackend: session map unreadable (%s); treating as empty",
                _exc_label(e),
            )
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _persist_session_id(self, session_id: str, candidate: str) -> str:
        """Record ``candidate`` for this host key and return the effective id.

        Read-modify-write, and an id already on disk wins over the candidate:
        two processes starting on the same host session key at once must
        converge on one everos session rather than each keeping its own, which
        is the whole point of persisting the map. Best-effort — a workspace
        that cannot be written falls back to process-local behavior rather
        than failing the turn.
        """
        path = self._session_map_path()
        if path is None:
            return candidate
        key = self._session_map_key(session_id)
        try:
            mapping = self._read_session_map()
            existing = mapping.get(key)
            if existing is not None:
                return existing
            mapping[key] = candidate
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(mapping, indent=1), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            self._logger.warning(
                "EverosBackend: failed to persist session map (%s); this "
                "session will not be resumable by a later process",
                _exc_label(e),
            )
        return candidate

    @staticmethod
    def _read_recorded_sessions(path: Path) -> list[dict[str, str]]:
        """Rows already in the sidecar. Unreadable / malformed lines are
        skipped rather than raised on: this only feeds a duplicate check, so a
        bad line costs a redundant append, never a lost record."""
        if not path.exists():
            return []
        rows: list[dict[str, str]] = []
        with contextlib.suppress(OSError):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(ValueError):
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        rows.append(parsed)
        return rows

    def _record_deferred_session(self, everos_session: str) -> None:
        """Sidecar record of deferred sessions for the post-hoc flush.

        Deferred capture leaves the buffer unpromoted; whoever flushes
        later needs (session_id, app_id, project_id). everos exposes no
        buffer-enumeration endpoint, so the writer records them —
        ``scripts/everos_flush_batch.py`` consumes this file.
        """
        if not self._defer_extraction or everos_session in self._recorded_sessions:
            return
        self._recorded_sessions.add(everos_session)
        workspace = getattr(self._services, "workspace", None)
        if workspace is None:
            return
        try:
            row = {
                "session_id": everos_session,
                "app_id": self._app_id or "default",
                "project_id": self._project_id or "default",
            }
            path = Path(workspace) / ".everos_sessions.jsonl"
            # The in-memory set only dedupes within one process. Several ``-m``
            # calls sharing one host session key are separate processes that
            # resolve to the same everos session, so each would append the same
            # row again. The reader dedupes, so this is about not growing a
            # file without bound.
            if any(existing == row for existing in self._read_recorded_sessions(path)):
                return
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            self._logger.warning(
                "EverosBackend: failed to record deferred session (%s)",
                e,
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
                # ``summary`` is a hard 200-character prefix of ``episode``
                # (verified against everos 1.2.x, which cuts it mid-word), so
                # it is the fallback, never the choice: preferring it hands
                # the prompt a sentence chopped at an arbitrary byte.
                text = getattr(ep, "episode", "") or getattr(ep, "summary", "") or ""
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
        capture_max_chars: int = _DEFAULT_CAPTURE_MAX_CHARS,
        emit_user_messages: bool = True,
        capture_reasoning: bool = False,
        capture_reasoning_max_chars: int = _DEFAULT_REASONING_MAX_CHARS,
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
          ``recall(user_id=<X>)`` must use that same ``<X>``. Coerced
          into the path-safe charset (it becomes an on-disk owner dir).

        Timestamps are coerced to ms-epoch ints (AgentLoop stamps ISO
        strings — see :func:`_timestamp_ms`); missing/unparseable ones
        are filled with ``base_ms + position``. The result is forced
        strictly increasing — not one shared "now": AgentLoop stamps
        adjacent records with the same string, and the server's dedup
        key is ``(session_id, timestamp_ms, index-within-request)``, so
        a shared timestamp makes every chunk after the first collide
        and be silently dropped, and breaks cross-chunk message ordering.

        Other conversions: drop ``system``; drop ``user`` when
        ``emit_user_messages`` is off (a sub-agent's forwarded user text
        would double-extract under the same owner as the parent's);
        missing ``sender_id`` on a user message → ``user_id``;
        multimodal ``content`` → space-joined text; empty text → drop;
        content clipped to ``capture_max_chars`` (0 disables).

        ``capture_reasoning`` folds ``reasoning_content`` into the text of
        assistant messages **that carry tool_calls**, and only those. Two
        reasons for that restriction: the user track's renderer keeps only
        plain chat messages, so a tool-calling assistant row is invisible
        to it and folded deliberation cannot reach a profile or a fact —
        it reaches only the agent track's case extraction, which is the
        consumer that wants it. And a textless tool-calling row (a quarter
        of the assistant rows in real DR traffic) is exactly the row whose
        whole reasoning currently arrives empty.
        """
        base_ms = int(time.time() * 1000)
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            if role == "user" and not emit_user_messages:
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
            if capture_reasoning and tool_calls:
                content = _fold_reasoning(
                    content,
                    m.get("reasoning_content"),
                    capture_reasoning_max_chars,
                )
            # After the fold, so the two budgets compose instead of one
            # silently overriding the other.
            if capture_max_chars > 0 and len(content) > capture_max_chars:
                content = content[:capture_max_chars]
            if not content and not tool_calls:
                continue
            tool_call_id = m.get("tool_call_id") if role == "tool" else None
            if role == "tool" and not tool_call_id:
                # The server maps a tool row to a ToolCallResult and raises
                # when it has no id to pair on, which fails the whole
                # /flush with a 500 — every episode, fact and profile for
                # the session, not just this row. Since /add accepts the
                # row happily, that failure would surface only at
                # promotion time, possibly in a post-batch script over
                # many sessions. Dropping one unpairable row is the
                # cheaper loss. Every host producer sets the id, so this
                # is a guard, not a code path we expect to take.
                logger.warning(
                    "EverosBackend: dropping tool message with no tool_call_id "
                    "(name=%r); the server cannot pair it and would reject the "
                    "session's whole flush",
                    m.get("name"),
                )
                continue
            if role in ("assistant", "tool"):
                sender = agent_id
            else:
                sender = _sanitize_path_safe(
                    str(m.get("sender_id") or user_id),
                    user_id,
                )
            ts = _timestamp_ms(m.get("timestamp"), base_ms + len(out))
            if out and ts <= out[-1]["timestamp"]:
                ts = out[-1]["timestamp"] + 1
            entry: dict[str, Any] = {
                "sender_id": sender,
                "role": role,
                "timestamp": ts,
                "content": content,
            }
            if tool_calls:
                entry["tool_calls"] = tool_calls
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
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


__all__ = ["EverosBackend", "_HttpEverosAdapter", "make_backend"]
