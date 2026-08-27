"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path
from collections.abc import Callable
from typing import Any

from loguru import logger

from raven.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.shell import ExecTool
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.config.schema import ExecToolConfig
from raven.providers.base import LLMProvider
from raven.sandbox import SandboxConfig, build_executor
from raven.security.benchmark_containment import BenchmarkContainment
from raven.security.trust import wrap_untrusted
from raven.tracing import semconv, trace
from raven.utils.helpers import build_assistant_message

# One hour: a runaway re-injection loop fires fast and trips the limit quickly,
# while legitimate spawns spread over time and age out before it bites.
_SPAWN_WINDOW_SECONDS = 3600


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        web_corpus_endpoint: str | None = None,
        benchmark_containment: "BenchmarkContainment | None" = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        sandbox_config: "SandboxConfig | None" = None,
        owned_ids: set[str] | None = None,
        jina_api_key: str | None = None,
        max_concurrent: int = 4,
        max_spawns_per_hour: int = 30,
        parent_tool_names: "Callable[[], list[str]] | None" = None,
    ):
        # dr@2.3: a CALLABLE, not a snapshot - the parent's registry is empty at
        # construction time, so a snapshot would fence against nothing. See
        # _build_subagent_tools for why an empty result is read as "not yet populated".
        self.parent_tool_names = parent_tool_names
        self.last_surface_dropped: list[str] = []
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
        # Must be inherited from the parent loop: a fixed-corpus benchmark run is void the
        # moment any subagent reaches the live web, and the subagent tool registry is built
        # here, not by the parent.
        self.web_corpus_endpoint = web_corpus_endpoint
        # Same inheritance requirement, and the parent's instance rather than a
        # fresh one: a subagent's blocks have to land in the arm's single count.
        self.benchmark_containment = benchmark_containment or BenchmarkContainment()
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self._sandbox_config = sandbox_config
        self._owned_ids = owned_ids
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        # Report pool: task_id -> {label, session_key, status, result}. Unlike
        # ``_running_tasks`` (pruned on completion), entries survive so results
        # are consumable after the fact; terminal entries are capped per
        # session. States: queued -> in-progress -> ready | failed.
        self._reports: dict[str, dict[str, Any]] = {}
        self._gate = asyncio.Semaphore(max_concurrent)
        self._max_spawns_per_hour = max_spawns_per_hour
        # Per-session spawn timestamps (monotonic), kept per session (not
        # per-process) so one busy session can't throttle others. Each deque is
        # pruned to the rolling window on access, so it self-bounds.
        self._session_spawn_times: dict[str, deque[float]] = {}

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
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
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": quota_key}

        self._reports[task_id] = {
            "task_id": task_id,
            "label": display_label,
            "session_key": quota_key,
            "status": "queued",
            "result": None,
        }
        bg_task = asyncio.create_task(self._run_subagent(task_id, task, display_label, origin))
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    @trace.instrument("subagent.run", extract=semconv.subagent)
    def _build_subagent_tools(self, executor: Any) -> ToolRegistry:
        """The subagent's registry. Extracted so the containment rule is testable:
        a capability fence that only exists inside a coroutine cannot be verified."""
        # Build subagent tools (no message tool, no spawn tool).
        #
        # A fixed-corpus run is a capability contract, not a name list: the
        # parent asserts containment over its own registry, but this registry is
        # built here and neither the parent's disabledTools nor its
        # drFlow.toolsAllowlist reaches it. Registering a subprocess tool here
        # would reopen the exec+curl egress that voided a third-party comparison
        # arm - the parent surface only looks safe today because `spawn` is
        # denied, i.e. one config line away. So when a corpus endpoint is set,
        # this registry is exactly the parent's DR surface and nothing else.
        #
        # Benchmark containment counts as the same kind of contract for the same
        # reason, and it is the case where the fence is easier to miss: that axis
        # has no corpus endpoint, so `contained` would be False and a subprocess
        # tool would be registered - after which `curl` reaches the dataset file
        # the web tools were just taught to refuse. A fence that one tool honors
        # and another walks around is not a fence.
        contained = self.web_corpus_endpoint is not None or self.benchmark_containment.enabled
        tools = ToolRegistry()
        if not contained:
            allowed_dir = self.workspace if self.restrict_to_workspace else None
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))
            tools.register(
                ExecTool(
                    working_dir=str(self.workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                    executor=executor,
                )
            )
        # Withheld without a key, same as the main loop: a sub-agent that reaches
        # for a search it cannot run reports the failure to its caller, and that
        # text ends up in the parent turn. A configured corpus endpoint is the
        # second source, and needs no key.
        web_search = WebSearchTool(
            api_key=self.brave_api_key,
            proxy=self.web_proxy,
            corpus_endpoint=self.web_corpus_endpoint,
            containment=self.benchmark_containment,
        )
        if web_search.api_key or web_search.corpus_endpoint:
            tools.register(web_search)
        tools.register(
            WebFetchTool(
                api_key=self.jina_api_key,
                proxy=self.web_proxy,
                corpus_endpoint=self.web_corpus_endpoint,
                containment=self.benchmark_containment,
            )
        )

        # ★★★ dr@2.3: the subagent surface must be a SUBSET of the parent's.
        #
        # ``contained`` above covers the two measurement fences (fixed corpus, benchmark
        # containment). It does not cover the ordinary case: an arm that denies ``exec``
        # and the file tools through ``disabledTools`` still handed its sub-agents all
        # five, because neither ``disabledTools`` nor ``drFlow.toolsAllowlist`` reaches
        # this registry. The parent's denial was therefore advisory - one ``spawn`` away
        # from being void - and a test in this tree asserted that as expected behaviour.
        #
        # The fence has to be a capability subset, not a name blacklist: enumerate what
        # the parent actually has and drop everything else. ``parent_tool_names`` is a
        # CALLABLE, never a snapshot, because the parent's registry is still empty when
        # this manager is constructed - the same trap that makes ``undeclared_tool`` fire
        # on nothing, since it reads the live registry at turn end.
        #
        # An empty parent surface means "not populated yet", not "parent has nothing", so
        # it is not treated as a denial - filtering on it would strip every tool and the
        # failure would look like a broken sub-agent rather than a broken fence.
        dropped: list[str] = []
        if self.parent_tool_names is not None:
            try:
                parent = set(self.parent_tool_names() or ())
            except Exception:
                parent = set()
            if parent:
                for name in sorted(tools.names()):
                    if name not in parent:
                        tools.unregister(name)
                        dropped.append(name)
        self.last_surface_dropped = dropped

        if contained:
            escapes = sorted(n for n in tools.names() if n in ("exec", "spawn") or n.startswith("mcp_"))
            if escapes:
                raise ValueError(
                    "subagent registry is not corpus-contained: "
                    f"{escapes} can reach outside the fixed corpus"
                )
        return tools

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
                if (report := self._reports.get(task_id)) is not None:
                    report["status"] = "in-progress"
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
        try:
            tools = self._build_subagent_tools(executor)

            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None

            while iteration < max_iterations:
                iteration += 1

                response = await self.provider.chat_with_retry(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=self.model,
                )

                if response.has_tool_calls:
                    tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
                    messages.append(
                        build_assistant_message(
                            response.content or "",
                            tool_calls=tool_call_dicts,
                            reasoning_content=response.reasoning_content,
                            thinking_blocks=response.thinking_blocks,
                        )
                    )

                    # Execute tools
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug(
                            "Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str
                        )
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        # The subagent's loop is an untrusted-data path too — fence its
                        # tool output like the main loop does in add_tool_result.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "content": wrap_untrusted(result, source=tool_call.name),
                            }
                        )
                else:
                    final_result = response.content
                    break

            if final_result is None:
                final_result = "Task completed but no final response was generated."

            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")

        except Exception as e:
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

        if (report := self._reports.get(task_id)) is not None:
            report["status"] = "ready" if status == "ok" else "failed"
            report["result"] = fenced_result
            self._prune_reports(report["session_key"])
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
        """Build a focused system prompt for the subagent."""
        from raven.agent.context import ContextBuilder
        from raven.memory_engine.skill_forge import LocalSkillCatalog

        # Use a transient ContextBuilder to access the runtime-context
        # builder; SubagentManager doesn't have its own ContextBuilder.
        time_ctx = ContextBuilder(self.workspace, start_watcher=False)._build_runtime_context(None, None)
        parts = [
            f"""# Subagent

{time_ctx}

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Workspace
{self.workspace}"""
        ]

        skills_summary = LocalSkillCatalog(
            self.workspace,
            start_watcher=False,
        ).build_skills_summary()
        if skills_summary:
            parts.append(f"## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n{skills_summary}")

        return "\n\n".join(parts)

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

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    _REPORT_POOL_MAX_TERMINAL_PER_SESSION = 32

    def report_pool(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Snapshot of the shared report pool, insertion-ordered.

        Each entry: ``{task_id, label, session_key, status, result}`` with
        ``status`` in queued / in-progress / ready / failed and ``result``
        the fenced payload once terminal. Consumers get copies — the pool
        itself is only mutated by the manager.
        """
        return [dict(r) for r in self._reports.values() if session_key is None or r["session_key"] == session_key]

    def _prune_reports(self, session_key: str) -> None:
        terminal = [
            tid
            for tid, r in self._reports.items()
            if r["session_key"] == session_key and r["status"] in ("ready", "failed")
        ]
        for tid in terminal[: -self._REPORT_POOL_MAX_TERMINAL_PER_SESSION]:
            del self._reports[tid]
