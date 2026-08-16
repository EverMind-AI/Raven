"""EverosBackend — HTTP-only memory backend.

The backend is the host's :class:`MemoryBackend` implementation,
delegating to a running EverOS server over HTTP
(``POST /api/v2/memory/{search,add,...}``).

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
import re
import time
from enum import Enum
from types import SimpleNamespace
from typing import Any, Literal, Protocol

import httpx

from raven.memory_engine import Memory
from raven.plugin import PluginContext
from raven.plugin.memory.everos._server import DEFAULT_EVEROS_BASE_URL

logger = logging.getLogger("raven.plugin.memory.everos")

_OwnerType = Literal["user", "agent"]

_PATH_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_.@+-]+$")
_PATH_TRAVERSAL_IDS = frozenset({".", ".."})
_STALE_IDENTITY_KEYS = ("user_id", "agent_id")


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


_DEFAULT_HTTP_TIMEOUT_S: float = 60.0
_MEMORIZE_TIMEOUT_S: float = 360.0

# Per-operation budgets. One flat 60s covered both reads and writes, which made
# every turn hostage to a service that answers slowly or not at all. These are
# sized by what the caller loses when they run out: a read that overruns costs
# the turn its recalled memory, a write that overruns costs that turn's memory
# permanently, and neither is worth a minute of the user's time.
_RECALL_TIMEOUT_S: float = 8.0
_STORE_TIMEOUT_S: float = 10.0


class ServiceState(Enum):
    """Whether the memory service is usable, and what would change that.

    Two axes are folded into one enum because callers only ever act on the
    combination: may I send a request, and is it worth probing again. The
    states that answer "no" to both -- ``UNCONFIGURED`` and ``NO_BINARY`` --
    describe the installation rather than the process, so no amount of probing
    resolves them and a stray success must not clear them.
    """

    UNKNOWN = "unknown"
    READY = "ready"
    STARTING = "starting"
    FAILED = "failed"
    UNRESPONSIVE = "unresponsive"
    UNCONFIGURED = "unconfigured"
    NO_BINARY = "no_binary"
    FOREIGN = "foreign"


# Probing cannot change these: they are facts about the install, not the
# process. Letting a probe promote out of them would hide a missing binary
# behind somebody else's server answering on the same port.
_TERMINAL_STATES = frozenset({ServiceState.UNCONFIGURED, ServiceState.NO_BINARY})

# Only the opening state may spawn. Every other non-ready state has already
# either spawned once (STARTING / FAILED), found the data occupied
# (UNRESPONSIVE), been told not to (FOREIGN), or knows a spawn cannot succeed.
_SPAWNABLE_STATES = frozenset({ServiceState.UNKNOWN})

# Minimum gap between out-of-band probes. Coarse on purpose: this exists to
# stop a task per turn from piling up, not to schedule anything.
_PROBE_MIN_INTERVAL_S: float = 2.0


class _HttpEverosAdapter:
    """Adapter that talks to a remote EverOS service over HTTP.

    Endpoints (see ``everos/entrypoints/api/routes/{search,memorize}.py``).
    ``/api/v2`` is the canonical prefix as of everos 1.2.0; ``/api/v1`` still
    resolves to the same handlers but is documented as a legacy alias that a
    future major release may drop:

    - ``POST /api/v2/memory/search`` — request body ``SearchRequest``,
      response ``{request_id, data: SearchData}``.
    - ``POST /api/v2/memory/add`` — request body ``MemorizeAddRequest``,
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
        self._caps: dict[str, bool] | None = None

    async def aclose(self) -> None:
        """Close the underlying client if we own it. Idempotent."""
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    async def _capabilities(self) -> dict[str, bool]:
        """What the server built, from ``/health``, cached for this adapter.

        everos reports ``capabilities`` as of 1.2.1. An empty mapping means the
        question could not be answered -- the server is unreachable, or predates
        the field -- and every caller must then leave the request alone: a
        ``SearchRequest`` forbids extra keys, so guessing turns a working request
        into a validation error.

        Cached because everos documents a tier change as requiring a server
        restart, and the adapter does not outlive one.
        """
        if self._caps is None:
            self._caps = await self._probe_capabilities()
        return self._caps

    async def _probe_capabilities(self) -> dict[str, bool]:
        from raven.plugin.memory.everos._health import HEALTH_TIMEOUT_S, parse_capabilities

        try:
            # Same headers as every other call on this client: everos ships no
            # auth today, but a deployment behind a proxy that adds it would read
            # an unauthenticated probe as a server with no capabilities.
            r = await self._client.get(
                f"{self._base_url}/health",
                headers=self._headers(),
                timeout=HEALTH_TIMEOUT_S,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            logger.warning("everos health probe failed (%s); leaving search parameters untouched", exc)
            return {}
        return parse_capabilities(payload)

    async def _search_tuning(self, *, agent_id: str | None) -> dict[str, Any]:
        """Search parameters this server's capabilities call for.

        Two degradations, and the first rules out the second:

        - **No embedding.** The default HYBRID is refused outright (everos
          ``needs_embedding`` covers vector, hybrid and agentic), so recall would
          return nothing at all. KEYWORD needs neither embedding nor rerank and
          still searches the same rows, just lexically -- worse recall, but
          recall. This is what makes the embedding role genuinely optional.
        - **No rerank, agent track, HYBRID.** Agent-track HYBRID fuses
          ``agent_case`` / ``agent_skill`` through a cross-encoder; without one
          the server refuses the request. ``enable_llm_rerank`` is the documented
          fallback, and it costs one LLM call per recall, so it is only taken
          when the cross-encoder is genuinely absent. Moot under KEYWORD, whose
          agent path does not go through rerank at all.
        """
        caps = await self._capabilities()
        if caps.get("embed") is False:
            return {"method": "keyword"}
        if agent_id is not None and caps.get("rerank") is False:
            return {"enable_llm_rerank": True}
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
            # Profiles are opt-in server-side and default off, so without this
            # every extracted user profile stays unreachable. It costs nothing
            # server-side: a direct fetch, not ranked, not counted against
            # top_k, at most one row. It is not free in the prompt — see
            # _PROFILE_MAX_CHARS. Agent owners ignore the flag, so only send
            # it for user_id.
            body["include_profile"] = True
        if agent_id is not None:
            body["agent_id"] = agent_id
        body.update(await self._search_tuning(agent_id=agent_id))
        url = f"{self._base_url}/api/v2/memory/search"
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
        url = f"{self._base_url}/api/v2/memory/add"
        r = await self._client.post(url, json=body, headers=self._headers(), timeout=_MEMORIZE_TIMEOUT_S)
        r.raise_for_status()
        if is_final:
            flush_body: dict[str, Any] = {"session_id": session_id}
            if app_id is not None:
                flush_body["app_id"] = app_id
            if project_id is not None:
                flush_body["project_id"] = project_id
            flush_url = f"{self._base_url}/api/v2/memory/flush"
            fr = await self._client.post(
                flush_url,
                json=flush_body,
                headers=self._headers(),
                timeout=_MEMORIZE_TIMEOUT_S,
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
        self._agent_id: str = self._services.agent_id
        self._user_id: str = self._services.user_id
        self._warn_stale_identity_keys()
        self._flush_every_turns: int = int(
            self._config.get("flush_every_turns", 1),
        )
        self._turn_counts: dict[str, int] = {}
        self._feedback_noop_logged = False
        # An injected adapter comes from a caller supplying its own transport,
        # which is also a caller that owns whatever is on the other end: there
        # is no server for this backend to probe or spawn. Production never
        # takes this branch (``make_backend`` passes no adapter), so the state
        # machine still governs every real session.
        self._state: ServiceState = ServiceState.READY if adapter is not None else ServiceState.UNKNOWN
        # The child raven spawned, when it spawned one. Kept past the start
        # window so a later failure can ask "is it still booting or did it
        # die" instead of guessing from how long it has been.
        self._proc: Any | None = None
        self._reported: set[ServiceState] = set()
        self._probe_task: asyncio.Task | None = None
        self._last_probe_at: float = 0.0
        self._store_inflight: set[asyncio.Task] = set()
        self._dropped_writes = 0

        if adapter is not None:
            self._adapter: _Adapter | None = adapter
        else:
            self._adapter = self._make_http_adapter()

    def _make_http_adapter(self) -> _Adapter:
        """Construct an :class:`_HttpEverosAdapter` from plugin config.

        Pulls ``base_url`` / ``api_key`` / ``timeout_s`` out of
        ``ctx.config`` with documented defaults.
        """
        base_url = self._config.get("base_url") or DEFAULT_EVEROS_BASE_URL
        api_key = self._config.get("api_key")
        timeout_s = float(
            self._config.get("timeout_s", _DEFAULT_HTTP_TIMEOUT_S),
        )
        return _HttpEverosAdapter(
            base_url,
            api_key=api_key,
            timeout_s=timeout_s,
        )

    # ── Service state ───────────────────────────────────────────────

    def _apply_probe(self, result: Any) -> None:
        """Move the state to whatever the probe just proved.

        ``REFUSED`` is the only result that needs a second question. Nothing is
        listening, but that is true both of a child still binding its port and
        of one that exited a second ago, and the two want opposite responses --
        wait, or stop and report. The child's exit code separates them; there is
        no timing heuristic that does.
        """
        from raven.plugin.memory.everos._server import ProbeResult

        if self._state in _TERMINAL_STATES:
            return
        if result is ProbeResult.OK:
            self._state = ServiceState.READY
            return
        if result is ProbeResult.TIMEOUT:
            self._state = ServiceState.UNRESPONSIVE
            return
        if result is ProbeResult.REFUSED:
            self._state = self._state_from_child()
            return
        self._state = ServiceState.UNRESPONSIVE

    def _state_from_child(self) -> ServiceState:
        """``STARTING`` or ``FAILED``, per the spawned child's exit code.

        ``None`` means no child of ours: either nothing was spawned yet, or
        another process holds the spawn lock and is starting one. Neither is a
        failure of ours to report, so both read as still starting.
        """
        if self._proc is None or self._proc.poll() is None:
            return ServiceState.STARTING
        return ServiceState.FAILED

    def _may_spawn(self) -> bool:
        """Whether starting a server could still help.

        Guards against the loop where a child that dies on startup is spawned
        again on the next turn, and again, filling the log with identical
        tracebacks while the user waits.
        """
        return self._state in _SPAWNABLE_STATES

    def _should_report(self) -> bool:
        """True once per state per session, so a warning stays a warning."""
        if self._state in self._reported:
            return False
        self._reported.add(self._state)
        return True

    def _kick_probe(self) -> None:
        """Start an out-of-band probe, if one is not already due or running.

        Fire-and-forget on purpose: the caller has already decided this turn
        has no memory, and making it wait for confirmation would reintroduce
        the stall the state machine exists to remove. The result lands in
        ``_state`` and the next call benefits.
        """
        import time as _time

        if self._state in _TERMINAL_STATES:
            return
        if self._probe_task is not None and not self._probe_task.done():
            return
        now = _time.monotonic()
        if now - self._last_probe_at < _PROBE_MIN_INTERVAL_S:
            return
        self._last_probe_at = now
        try:
            self._probe_task = asyncio.get_running_loop().create_task(self._probe_once())
        except RuntimeError:  # no running loop (sync context / teardown)
            self._probe_task = None

    async def _probe_once(self) -> None:
        from raven.plugin.memory.everos._server import probe_health

        base_url = self._config.get("base_url") or DEFAULT_EVEROS_BASE_URL
        result = await asyncio.to_thread(probe_health, base_url)
        self._apply_probe(result)

    def _demote_from_exception(self, exc: BaseException) -> None:
        """Classify a request failure the same way a probe would.

        A read that fails and a probe that fails are the same observation
        arriving through different doors, so they must not disagree about what
        state the service is in.
        """
        import httpx

        from raven.plugin.memory.everos._server import ProbeResult

        if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
            self._apply_probe(ProbeResult.TIMEOUT)
        elif isinstance(exc, httpx.ConnectError):
            self._apply_probe(ProbeResult.REFUSED)
        else:
            self._apply_probe(ProbeResult.ERROR)

    def _warn_stale_identity_keys(self) -> None:
        """Surface a config left over from before identity moved to the host.

        A stale value that differs from the host's is exactly the split that
        used to make every written memory unrecallable, so it must be loud
        rather than silently ignored.
        """
        for key in _STALE_IDENTITY_KEYS:
            stale = self._config.get(key)
            if stale is None:
                continue
            current = self._user_id if key == "user_id" else self._agent_id
            if stale != current:
                self._logger.warning(
                    "plugins.config['everos-memory'].%s=%r is obsolete and ignored; "
                    "the active value is memory.%s=%r. Remove the stale key.",
                    key,
                    stale,
                    "userId" if key == "user_id" else "agentId",
                    current,
                )

    def _validate_identity(self) -> None:
        # Name the on-disk camelCase key, not the Python attribute: the message
        # has to be greppable in the user's config.json.
        for key, value in (("userId", self._user_id), ("agentId", self._agent_id)):
            if value in _PATH_TRAVERSAL_IDS or not _PATH_SAFE_ID_RE.match(value):
                raise ValueError(
                    f"memory.{key}={value!r} is not accepted by EverOS: it becomes a "
                    f"directory segment on the write path, so it must match "
                    f"{_PATH_SAFE_ID_RE.pattern} and must not be '.' or '..'."
                )

    # ── Lifecycle ───────────────────────────────────────────────────

    async def start(self) -> None:
        self._validate_identity()
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

            from rich.console import Console

            from raven.config.update_everos import everos_owned
            from raven.plugin.memory.everos._server import (
                EverosBinaryMissingError,
                EverosNotConfiguredError,
                ensure_everos_server,
            )

            stderr = Console(stderr=True)
            base_url = self._config.get("base_url") or DEFAULT_EVEROS_BASE_URL

            if not everos_owned():
                # A root the user manages: connect if a server is up, never start
                # one. Starting it would take the OME jobstore lock exclusively,
                # which is theirs to grant, not raven's to assume.
                from raven.plugin.memory.everos._server import ProbeResult, probe_health

                if await asyncio.to_thread(probe_health, base_url) is ProbeResult.OK:
                    self._state = ServiceState.READY
                    # Say what it can actually do, exactly as the owned path
                    # does. The argument for the warning is stronger here, not
                    # weaker: raven cannot repair someone else's embedding
                    # config, so telling them is the only move it has.
                    await asyncio.to_thread(self._warn_if_recall_cannot_work, base_url)
                    return
                # FOREIGN, not NoOp: raven still must not start this server, but
                # the user may start it themselves mid-session, and the probe
                # that notices needs an adapter left to use.
                self._state = ServiceState.FOREIGN
                stderr.print(
                    f"[yellow]Long-term memory is off: the EverOS you manage is not running at {base_url}.[/yellow]\n"
                    "[dim]Start it yourself and Raven will use it; Raven does not start or stop it.[/dim]"
                )
                return

            try:
                # Narrate only a real wait. ``on_wait`` does not fire when a
                # server is already answering, which is the common case -- a line
                # there would be noise on every single session, and a healthy
                # start is meant to be silent.
                self._proc = await ensure_everos_server(
                    base_url,
                    on_wait=lambda: stderr.print("[dim]Starting memory service...[/dim]"),
                )
                self._state = ServiceState.READY
            except EverosNotConfiguredError:
                # Reachable out of the box: memory.backend defaults to "everos"
                # while the shipped everos.toml has an empty [llm] api_key. The
                # user can act on this, so say it here rather than only in the
                # log the caller writes.
                self._state = ServiceState.UNCONFIGURED
                stderr.print(
                    "[yellow]Long-term memory is off: its LLM is not configured.[/yellow]\n"
                    "[dim]Run `raven onboard` to set it up.[/dim]"
                )
                return
            except EverosBinaryMissingError as e:
                # An install problem, not a startup problem: no probe and no
                # retry can resolve it, so it must not be filed with the states
                # that keep trying.
                self._state = ServiceState.NO_BINARY
                stderr.print(
                    f"[yellow]Long-term memory is off: {e}[/yellow]\n"
                    "[dim]Install the everos CLI, then start a new session.[/dim]"
                )
                return
            except Exception as e:
                # Not raised on: the session continues without memory, and the
                # state machine keeps probing in case the server comes up. The
                # old ``raise`` cost the caller a traceback for a degradation it
                # already handles.
                self._state = self._state_from_child()
                self._logger.error(
                    "EverosBackend: failed to start EverOS server (%s); state=%s",
                    e,
                    self._state.value,
                )
                stderr.print(
                    f"[yellow]Memory service unavailable: {e}[/yellow]\n"
                    "[dim]This session starts without long-term memory; Raven retries in the background.[/dim]"
                )
                return
            # Off-thread: the probe and the config read below are both blocking
            # IO, and start() runs on the loop every session begins on.
            await asyncio.to_thread(self._warn_if_recall_cannot_work, base_url)

    @staticmethod
    def _warn_if_recall_cannot_work(base_url: str) -> None:
        """Say out loud when the server is up but recall is not what it should be.

        A running server no longer implies a fully working one: everos 1.2.1 boots
        with ``[llm]`` alone. What is left is real but weaker, and the log saying
        so is file-only at runtime, so the difference looks like an agent that is
        merely vague rather than one running on a lesser search -- the hardest
        kind of fault to attribute. ``raven doctor`` can find it, but only if the
        user thinks to ask; this is on the path every session already takes.

        Only what was configured and could not be built is worth saying: a role
        the user never configured is a choice they already know about, and
        repeating it every start would be noise.
        """
        from raven.plugin.memory.everos._health import probe_capabilities

        report = probe_capabilities(base_url)
        from raven.config.update_everos import everos_role_configured

        if not (everos_role_configured("embedding") and report.available("embedding") is False):
            return
        from rich.console import Console

        from raven.plugin.memory.everos._server import server_log_path

        Console(stderr=True).print(
            "[yellow]EverOS is running but embedding is unavailable: recall falls back to "
            "keyword matching.[/yellow]\n"
            f"[dim]Memories are still stored. Check {server_log_path()}, fix the provider, "
            "then run `everos cascade backfill` to give existing rows their vectors.[/dim]"
        )

    async def stop(self) -> None:
        self._logger.info("EverosBackend.stop")
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
        if self._dropped_writes:
            # Said out loud, once, at the only moment it is still actionable.
            # A dropped write is a conversation the user will never be able to
            # recall, and until now that fact lived only in a log file the TUI
            # does not even print to the terminal.
            from rich.console import Console

            Console(stderr=True).print(
                f"[yellow]{self._dropped_writes} turn(s) were not written to long-term memory "
                "because the memory service was unavailable.[/yellow]"
            )
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
        if self._state is not ServiceState.READY:
            # Nothing to wait for and nothing to pay: the turn gets no memory,
            # and a probe goes out of band so the next turn might. This is what
            # replaces swapping in a no-op adapter, which ended the session's
            # chance of recovering the moment one start failed.
            self._kick_probe()
            return []
        try:
            data = await asyncio.wait_for(
                self._adapter.search(
                    user_id=user_id,
                    agent_id=agent_id,
                    query=query,
                    top_k=top_k,
                ),
                timeout=_RECALL_TIMEOUT_S,
            )
        except (Exception, asyncio.TimeoutError) as e:
            self._demote_from_exception(e)
            self._logger.warning(
                "EverosBackend.recall failed (%s); state=%s; returning empty",
                e,
                self._state.value,
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
        if self._state is not ServiceState.READY:
            # Counted rather than logged and forgotten: a dropped write is a
            # turn the user will never be able to recall, and the only place
            # that fact can still be told to them is the end of the session.
            self._dropped_writes += 1
            self._kick_probe()
            return
        if metadata and "is_final" in metadata:
            is_final = bool(metadata["is_final"])
        else:
            n = self._turn_counts.get(session_id, 0) + 1
            self._turn_counts[session_id] = n
            is_final = self._flush_every_turns > 0 and n % self._flush_every_turns == 0

        try:
            await asyncio.wait_for(
                self._adapter.memorize(
                    session_id,
                    payload,
                    is_final=is_final,
                    app_id=metadata.get("app_id") if metadata else None,
                    project_id=metadata.get("project_id") if metadata else None,
                ),
                timeout=_STORE_TIMEOUT_S,
            )
        except (Exception, asyncio.TimeoutError) as e:
            self._demote_from_exception(e)
            self._dropped_writes += 1
            self._logger.warning(
                "EverosBackend.store failed (%s); state=%s; this turn was not indexed",
                e,
                self._state.value,
            )

    async def feedback(self, signals: dict[str, Any]) -> None:
        """Deliberate no-op pending an upstream everos feedback sink.

        The host already collects ``skill_usage`` signals (which everos
        skills were injected / used in a turn) and dispatches them here.
        everos 1.2.1's HTTP surface still exposes no endpoint to consume
        them — its routes are get / health / knowledge / memorize /
        metrics / ome / search, and ``agent_skill.confidence`` lives in
        the persistence internals with no service-level write path — so
        signals are dropped until everos grows one. The method stays on the Protocol because it is
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


# EverOS accumulates the profile monotonically over an install's life with no
# server-side size limit (11,414 chars measured before it was rendered as prose,
# 5,172 after, on a still-young install), so without a client-side ceiling one
# growing blob can come to dominate the recalled-memory block. The ceiling is
# sized to roughly the combined budget of a full episode batch (each episode's
# summary is itself capped near 200 chars server-side and memory_top_k defaults
# to 5), so the profile stays substantial without outweighing everything else.
#
# Its score falling back to 1.0 sorts it above every similarity-scored episode,
# which is ordering only: the profile is a direct fetch that does not count
# against top_k, and the caller renders every hit, so nothing is displaced by
# it being first. Length was the whole exposure.
_PROFILE_MAX_CHARS = 1200


def _flatten_profile(profile_data: Any) -> str:
    """Render a profile dict as human-readable lines for prompt injection.

    Scalars render as ``key: value``. Lists render one bullet per item.
    Dict items only surface ``category``/``trait`` (label) and
    ``description`` (body) — an allowlist, not a denylist of the
    ``evidence``/``basis`` meta-narration fields EverOS attaches to
    explain *how* it inferred an item, which is not a fact about the
    user and must never reach the prompt. Non-dicts get ``str()``.

    The result is capped at ``_PROFILE_MAX_CHARS``; see that constant.
    """
    if not isinstance(profile_data, dict):
        return _cap_profile_text(str(profile_data))
    lines: list[str] = []
    for key, value in profile_data.items():
        # EverOS stamps a profile with ``*_ms`` epoch keys recording when it last
        # touched each part. That is bookkeeping about the store, not a fact about
        # the user, and rendering it spends prompt budget on raw millisecond ints.
        if key.endswith("_ms"):
            continue
        if isinstance(value, list):
            lines.extend(_flatten_profile_list(value))
        else:
            lines.append(f"{key}: {value}")
    return _cap_profile_text("\n".join(lines))


def _cap_profile_text(text: str) -> str:
    """Truncate ``text`` to ``_PROFILE_MAX_CHARS``, on a line boundary,
    with a visible marker rather than a silent cut."""
    if len(text) <= _PROFILE_MAX_CHARS:
        return text
    head, _, _ = text[:_PROFILE_MAX_CHARS].rpartition("\n")
    kept = head or text[:_PROFILE_MAX_CHARS]
    omitted = len(text) - len(kept)
    return f"{kept}\n[profile truncated, {omitted} chars omitted]"


def _flatten_profile_list(items: list[Any]) -> list[str]:
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"- {item}")
            continue
        label = item.get("category") or item.get("trait")
        body = item.get("description")
        if label and body:
            lines.append(f"- {label}: {body}")
        elif label or body:
            lines.append(f"- {label or body}")
    return lines


# ---------------------------------------------------------------------------
# Factory — entry-point target
# ---------------------------------------------------------------------------


def make_backend(ctx: PluginContext) -> EverosBackend:
    """Plugin entry-point factory. Called by :class:`PluginRegistry`
    after manifest activation. Sync construction only — async setup
    happens in ``EverosBackend.start()``."""
    from raven.config.update_everos import (
        configure_everos_env,
        ensure_everos_home,
        everos_owned,
        everos_root,
    )

    root = everos_root()
    configure_everos_env(root)
    # See tools.py: a root the user manages is read-only, template files
    # included.
    if everos_owned():
        ensure_everos_home(root)
    return EverosBackend(ctx)


__all__ = ["EverosBackend", "make_backend"]
