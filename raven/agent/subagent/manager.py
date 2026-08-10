"""Subagent manager for background task execution."""

import asyncio
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.backends import (
    ABORTED_ACTION_RESULT,
    AgentMeta,
    RavenLoopBackend,
    SubagentActionAbortedError,
    SubagentBackend,
    build_subagent_prompt,
    build_third_party_backend,
    enabled_third_party,
    third_party_agent_meta,
)
from raven.agent.subagent.instances import get_registry
from raven.config.schema import ExecToolConfig
from raven.providers.base import LLMProvider
from raven.sandbox import SandboxConfig, build_executor
from raven.security.trust import wrap_untrusted
from raven.tracing import semconv, trace

# One hour: a runaway re-injection loop fires fast and trips the limit quickly,
# while legitimate spawns spread over time and age out before it bites.
_SPAWN_WINDOW_SECONDS = 3600
# A status row is never worth failing -- or hanging -- a spawn over: the gate
# a wedged CLI subagent already holds must never stall behind a slow or
# corrupt registry write.
_REGISTRY_WRITE_TIMEOUT_S = 2.0


async def _write_spawn_status(session_key: str | None, agent: str | None, handle: str, status: str) -> None:
    """Best-effort registry write for one spawn's status, swallowing any failure.

    Only third-party spawns get a row: a default raven-loop subagent has no
    third-party agent name and must not appear as an instance row.
    """
    if not session_key or not agent:
        return
    try:
        await asyncio.wait_for(
            get_registry().upsert_spawn(session_key, agent, handle, status),
            timeout=_REGISTRY_WRITE_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a status row must never fail or hang a spawn
        logger.opt(exception=True).warning(
            "Subagent instance registry write failed for {}/{!r} (status={})", agent, handle, status
        )


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        sandbox_config: "SandboxConfig | None" = None,
        owned_ids: set[str] | None = None,
        jina_api_key: str | None = None,
        max_concurrent: int = 4,
        max_spawns_per_hour: int = 30,
        third_party_subagents: list | None = None,
    ):
        from raven.config.schema import ExecToolConfig

        self.provider = provider
        self.workspace = workspace
        # Spine submit, late-bound (the scheduler pins its home loop at
        # construction and is built inside each entry point's run loop; this
        # manager is built in AgentLoop.__init__ in the sync prologue). Wired via
        # set_submit before any announce; the result re-injection submits a
        # SUBAGENT-origin turn.
        self._submit = None
        self.model = model or provider.get_default_model()
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self._sandbox_config = sandbox_config
        self._owned_ids = owned_ids
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        # (session_key, agent, handle) -> {task_id, ...}, for one-instance
        # cancellation (a stop button) without touching the rest of the
        # session's spawns. A set, not a single id: the main agent can spawn
        # the same (agent, instance) twice concurrently before the first
        # completes, and cancel_by_instance must reach every one of them, not
        # just whichever spawn happened to overwrite the slot last.
        self._instance_tasks: dict[tuple[str, str, str], set[str]] = {}
        self._gate = asyncio.Semaphore(max_concurrent)
        self._max_spawns_per_hour = max_spawns_per_hour
        # Per-session spawn timestamps (monotonic), kept per session (not
        # per-process) so one busy session can't throttle others. Each deque is
        # pruned to the rolling window on access, so it self-bounds.
        self._session_spawn_times: dict[str, deque[float]] = {}
        # Pluggable per-spawn execution backend. Default is an in-process raven
        # loop; third-party backends (CLI claude/codex, OpenAI-API mirothinker;
        # req5) register in ``_backends`` keyed by agent name.
        self._raven_backend = RavenLoopBackend(
            provider=self.provider,
            model=self.model,
            restrict_to_workspace=self.restrict_to_workspace,
            exec_config=self.exec_config,
            brave_api_key=self.brave_api_key,
            jina_api_key=self.jina_api_key,
            web_proxy=self.web_proxy,
        )
        self._backends: dict[str, SubagentBackend] = {}
        # (name, description, stateful) of configured third-party agents, for
        # the spawn tool's description so the main agent knows what it can
        # dispatch to.
        self._third_party_meta: list[tuple[str, str, bool]] = []
        self.set_third_party_subagents(third_party_subagents or [])

    def set_third_party_subagents(self, configs: list) -> None:
        """(Re)build the third-party backend registry from config. Hot-appliable
        at runtime (P4): replaces the current set so a web config change takes
        effect without a restart."""
        backends: dict[str, SubagentBackend] = {}
        meta: list[AgentMeta] = []
        for cfg in enabled_third_party(configs):
            name = getattr(cfg, "name", None)
            try:
                backends[name] = build_third_party_backend(cfg)
                meta.append(third_party_agent_meta(cfg))
            except Exception as e:  # noqa: BLE001 — a bad entry must not sink the manager
                logger.warning("Skipping third-party subagent {!r}: {}", name, e)
        self._backends = backends
        self._third_party_meta = meta

    def _resolve_backend(self, agent: str | None) -> SubagentBackend:
        """Pick the execution backend for a spawn; unknown/None -> raven loop."""
        if agent and agent in self._backends:
            return self._backends[agent]
        return self._raven_backend

    def list_third_party_agents(self) -> list[AgentMeta]:
        """Advertised capabilities of the configured third-party agents (for the spawn tool)."""
        return list(self._third_party_meta)

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        agent: str | None = None,
        instance: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        quota_key = session_key or "default"
        now = time.monotonic()
        window = self._session_spawn_times.setdefault(quota_key, deque())
        cutoff = now - _SPAWN_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._max_spawns_per_hour:
            logger.warning(
                "Spawn refused: session {!r} hit spawn rate limit ({}/hour)",
                quota_key,
                self._max_spawns_per_hour,
            )
            return (
                f"Spawn refused: this session hit its subagent spawn rate limit "
                f"({self._max_spawns_per_hour} per hour). It recovers automatically "
                f"as earlier spawns age out — if this is unexpected, the task may "
                f"be looping; reconsider the approach instead of spawning again."
            )
        window.append(now)
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        # Computed once and carried in `origin` so the concurrency index below
        # and the registry rows this spawn later writes can never drift from
        # each other, or from `CliAgentBackend`'s own handle derivation.
        handle = instance or task_id
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": quota_key,
            "agent": agent,
            "instance": instance,
            "handle": handle,
        }
        instance_key = (quota_key, agent, handle) if agent else None

        # A row before the task even exists: a spawn queued behind a full gate
        # (or a sandbox VM still booting) would otherwise have no registry row
        # at all until it starts running, making it invisible and unstoppable
        # from the UI for however long it waits.
        if agent:
            await _write_spawn_status(session_key, agent, handle, "pending")

        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin))
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)
        if instance_key is not None:
            self._instance_tasks.setdefault(instance_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]
            if instance_key is not None and (ids := self._instance_tasks.get(instance_key)) is not None:
                ids.discard(task_id)
                if not ids:
                    del self._instance_tasks[instance_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    @trace.instrument("subagent.run", extract=semconv.subagent)
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        try:
            # Each subagent runs its own sandbox VM; gate the count so heavy
            # fan-out can't exhaust host resources.
            async with self._gate:
                executor = build_executor(self._sandbox_config, self.workspace, self._owned_ids)
                async with executor:
                    await self._run_subagent_inner(task_id, task, label, origin, executor)
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    async def _run_subagent_inner(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        executor: Any,
    ) -> None:
        session_key = origin.get("session_key")
        agent = origin.get("agent")
        handle = origin.get("handle") or task_id
        try:
            await _write_spawn_status(session_key, agent, handle, "running")
            backend = self._resolve_backend(agent)
            final_result = await backend.run(
                task,
                task_id=task_id,
                workspace=self.workspace,
                executor=executor,
                session_key=session_key,
                instance=origin.get("instance"),
            )
            await _write_spawn_status(session_key, agent, handle, "completed")
            await self._announce_result(task_id, label, task, final_result, origin, "ok")
        except asyncio.CancelledError:
            await _write_spawn_status(session_key, agent, handle, "cancelled")
            raise
        except SubagentActionAbortedError:
            await _write_spawn_status(session_key, agent, handle, "failed")
            logger.info("Subagent [{}] stopped on a terminal safety decision", task_id)
            await self._announce_result(task_id, label, task, ABORTED_ACTION_RESULT, origin, "error")
        except Exception as e:
            await _write_spawn_status(session_key, agent, handle, "failed")
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")

    def set_submit(self, submit) -> None:
        self._submit = submit

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the spine.

        Note: this inbound system message already triggers a main-agent turn,
        so subagent completion is event-driven end-to-end. Do NOT also
        enqueue a heartbeat SystemEvent here — that would process the same
        fact twice (double LLM cost, risk of double-notifying the user).
        """
        status_text = "completed successfully" if status == "ok" else "failed"

        # The subagent's result is attacker-influenceable (it may have fetched
        # web pages / read files), so fence it as untrusted before it re-enters
        # the main agent's context.
        fenced_result = wrap_untrusted(result, source="subagent")
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{fenced_result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

        # Re-inject to trigger a main-agent turn in the originating session. The
        # spine path routes by conversation (= originating session) with
        # origin=SUBAGENT; the reply rides emit -> hub -> outlet (source.channel
        # is the originating channel). Fire-and-forget — the announce is fixed,
        # the turn's output isn't read back.
        assert self._submit is not None
        from raven.spine import ChatType, Origin, Source, TurnRequest

        self._submit(
            TurnRequest(
                origin=Origin.SUBAGENT,
                source=Source(
                    channel=origin["channel"],
                    chat_id=origin["chat_id"],
                    sender_id="subagent",
                    chat_type=ChatType.DM,
                ),
                text=announce_content,
                conversation=origin["session_key"],
            )
        )
        logger.debug("Subagent [{}] announced result to {}", task_id, origin["session_key"])

    def _build_subagent_prompt(self) -> str:
        """Build a focused system prompt for the subagent (raven-loop backend)."""
        return build_subagent_prompt(self.workspace)

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(session_key, [])
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Drop this session's rate-limit entry on teardown: pruning empties a
        # deque but never removes the key, so without this the dict would keep
        # one entry per session for the process's life.
        self._session_spawn_times.pop(session_key, None)
        return len(tasks)

    async def cancel_by_instance(self, session_key: str, agent: str, handle: str) -> bool:
        """Cancel every spawn on one (session_key, agent, handle).

        This is the granularity a stop button needs: cancelling unwinds each
        `async with self._gate` the matched spawns are holding (or waiting
        on), releasing their concurrency slots, without touching the rest of
        the session's spawns. Usually there is exactly one live task per
        instance key, but two spawns can race onto the same key before the
        first completes, so this cancels *all* of them rather than only the
        most recent. Returns whether any live task was found.
        """
        quota_key = session_key or "default"
        task_ids = self._instance_tasks.get((quota_key, agent, handle), set())
        tasks = [t for tid in task_ids if (t := self._running_tasks.get(tid)) is not None and not t.done()]
        if not tasks:
            return False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return True

    def live_handles(self, session_key: str) -> set[tuple[str, str]]:
        """The (agent, handle) pairs currently in flight for one session.

        Lets a reader tell a genuinely running instance row from one orphaned
        by a gateway restart (the process-wide task index does not survive
        one, unlike the persistent registry).
        """
        quota_key = session_key or "default"
        live: set[tuple[str, str]] = set()
        for (skey, agent, handle), task_ids in self._instance_tasks.items():
            if skey != quota_key:
                continue
            if any((t := self._running_tasks.get(tid)) is not None and not t.done() for tid in task_ids):
                live.add((agent, handle))
        return live

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    async def cancel_all(self) -> int:
        """Cancel every subagent still running, across every session.

        Used at gateway shutdown: `CliAgentBackend` runs its child with
        `start_new_session=True` (its own process group), which detaches it
        from the gateway's process group, so a Ctrl-C to the gateway no
        longer reaches it. With every automatic timeout also removed, a
        wedged CLI child would otherwise be orphaned and keep running against
        the workspace forever once the gateway exits. Returns the count
        cancelled.
        """
        tasks = [t for t in self._running_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)
