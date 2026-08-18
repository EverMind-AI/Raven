"""The default subagent backend: a bounded in-process Raven agent loop.

This is the logic that used to live inline in ``SubagentManager._run_subagent_inner``
and ``_build_subagent_prompt``, extracted verbatim so a spawned sub-agent's
*executor* becomes pluggable without changing default behavior.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent import activity
from raven.agent.subagent.backends.base import IN_SUBAGENT_RUN, SubagentActionAbortedError
from raven.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.agent.tools.shell import ExecTool
from raven.agent.tools.web import WebFetchTool, WebSearchTool
from raven.config.schema import ExecToolConfig
from raven.memory_engine.skill_local.registry import filter_by_required_tools
from raven.providers.base import LLMProvider
from raven.security.trust import wrap_untrusted
from raven.utils.helpers import build_assistant_message


def build_subagent_prompt(
    agent_home: Path,
    work_dir: Path,
    tool_names: Collection[str] = (),
    skills_allow: Collection[str] | None = None,
) -> str:
    """Build a focused system prompt for an in-process raven subagent.

    ``agent_home`` is where the agent's memory and skills live; ``work_dir`` is
    the session directory this sub-agent reads and writes files in. They are
    different directories and only ``agent_home`` may reach the skill catalog
    or the memory store -- pointing those at ``work_dir`` would build a private
    raven tree inside the user's own directory and hide every skill the agent
    actually has.

    ``tool_names`` is what this sub-agent's registry actually holds; skills
    declaring a ``requires.tools`` outside it are withheld, so the prompt can
    never hand the sub-agent a procedure it has no tool to follow. That covers
    the orchestration guide (it needs ``run_subagent_dag``, which only the main
    agent registers) without naming it, and any future tool-gated skill for
    free.

    The default is an *empty* set rather than "unknown": a sub-agent prompt is
    always built next to its own registry, so no names means no tools -- unlike
    the main agent's segment builder, where a callable that fails to answer has
    to degrade to showing everything.

    ``skills_allow`` narrows the skills menu on top of the tool filter:
    ``None`` keeps the current full-catalog behaviour, ``[]`` hides the menu
    entirely, and a list shows only the named skills. It stacks with the tool
    filter rather than replacing it, so a whitelisted skill whose required
    tools this sub-agent lacks is still withheld.
    """
    from raven.agent.context import ContextBuilder
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    # Transient ContextBuilder just for the runtime-context builder; the
    # subagent has no ContextBuilder of its own (and must not start a watcher).
    time_ctx = ContextBuilder(agent_home, start_watcher=False)._build_runtime_context(None, None)
    parts = [
        f"""# Subagent

{time_ctx}

You are a subagent spawned by the main agent to complete a specific task.
Stay focused on the assigned task. Your final response will be reported back to the main agent.

## Directories
- Working directory: {work_dir} — files you produce go here; relative paths resolve here.
- Agent home: {agent_home} — the agent's memory and skills."""
    ]

    catalog = LocalSkillCatalog(agent_home, start_watcher=False)
    visible = filter_by_required_tools(catalog.registry.list_all(), tool_names)
    if skills_allow is not None:
        allowed = set(skills_allow)
        visible = [s for s in visible if s.name in allowed]
    skills_summary = catalog.build_skills_summary(only=visible) if visible else ""
    if skills_summary:
        parts.append(f"## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n{skills_summary}")

    return "\n\n".join(parts)


class RavenLoopBackend:
    """Runs the task as a bounded in-process Raven agent loop (the default).

    Streaming forwards *every* model call's text, including the preamble a call
    writes before reaching for a tool. Only the final answer is returned, so a
    live direct chat shows more than the record replays afterwards: the preamble
    is progress, and a run's evidence is the answer it arrived at. Withholding it
    is not an option -- whether a call is the last one is knowable only once it
    has finished, which is after the whole reply would have been buffered.
    """

    kind = "raven-loop"
    streams = True
    _MAX_ITERATIONS = 15

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        agent_home: Path,
        restrict_to_workspace: bool = False,
        exec_config: "ExecToolConfig | None" = None,
        brave_api_key: str | None = None,
        jina_api_key: str | None = None,
        web_proxy: str | None = None,
        tools_allow: Collection[str] | None = None,
        skills_allow: Collection[str] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        # Global, unlike the per-run ``workspace``: memory and skills are the
        # agent's identity and stay in one place whatever directory a session
        # works in.
        self.agent_home = Path(agent_home)
        self.restrict_to_workspace = restrict_to_workspace
        self.exec_config = exec_config or ExecToolConfig()
        self.brave_api_key = brave_api_key
        self.jina_api_key = jina_api_key
        self.web_proxy = web_proxy
        # Per-role capability whitelists (playbook roles build one backend per
        # role). None = current full set; [] = none; a list = only those.
        # skills_allow additionally stacks with the tool-based skill filter.
        self.tools_allow = set(tools_allow) if tools_allow is not None else None
        self.skills_allow = skills_allow

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        token = IN_SUBAGENT_RUN.set(True)
        try:
            return await self._run(
                task,
                task_id=task_id,
                workspace=workspace,
                executor=executor,
                session_key=session_key,
                instance=instance,
                provider=provider,
                model=model,
                history=history,
                on_messages=on_messages,
                on_delta=on_delta,
            )
        finally:
            IN_SUBAGENT_RUN.reset(token)

    async def _run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
        provider: LLMProvider | None = None,
        model: str | None = None,
        history: list[dict[str, Any]] | None = None,
        on_messages: Callable[[list[dict[str, Any]]], None] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        # Deferred: importing raven.agent.loop runs its package init, which pulls
        # in AgentLoop, which imports this package at module scope.
        from raven.agent.loop.streaming import generation_kwargs, stream_llm_call

        # The spawn's snapshot wins over the pair this backend was built with;
        # see ``SubagentBackend.run``. The constructor pair remains the fallback
        # for callers that drive a backend directly.
        provider = provider or self.provider
        model = model or self.model
        # Build subagent tools (no message tool, no spawn tool).
        tools = ToolRegistry()

        def allowed(name: str) -> bool:
            return self.tools_allow is None or name in self.tools_allow

        # Two roots, matching the main loop: the session directory the run works
        # in, and agent home, whose absolute paths this prompt hands out.
        allowed_dirs = (workspace, self.agent_home) if self.restrict_to_workspace else ()
        # follow_binding=False: this run is a background asyncio task that can
        # outlive the turn that spawned it, since SubagentManager.spawn captures
        # the workspace at spawn time, so its tools must fence on the directory
        # captured for this run, not on whatever the ambient workdir binding
        # holds by the time they actually execute.
        if allowed("read_file"):
            tools.register(ReadFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("write_file"):
            tools.register(WriteFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("edit_file"):
            tools.register(EditFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("list_dir"):
            tools.register(ListDirTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        if allowed("exec"):
            tools.register(
                ExecTool(
                    working_dir=str(workspace),
                    timeout=self.exec_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    path_append=self.exec_config.path_append,
                    executor=executor,
                    extra_deny_patterns=self.exec_config.extra_deny_patterns,
                    extra_allowed_dirs=allowed_dirs,
                    follow_binding=False,
                )
            )
        # Withheld without a key, same as the main loop: a sub-agent that reaches
        # for a search it cannot run reports the failure to its caller, and that
        # text ends up in the parent turn. The whitelist stacks on top: a
        # whitelisted web_search without a key is still withheld.
        if allowed("web_search"):
            web_search = WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy)
            if web_search.api_key:
                tools.register(web_search)
        if allowed("web_fetch"):
            tools.register(WebFetchTool(api_key=self.jina_api_key, proxy=self.web_proxy))

        # A resumed instance brings its own history, system prompt included;
        # rebuilding the prompt here would append a second system turn. A
        # fresh run's prompt carries the role's skill whitelist.
        messages: list[dict[str, Any]] = (
            list(history)
            if history
            else [
                {
                    "role": "system",
                    "content": build_subagent_prompt(self.agent_home, workspace, tools.tool_names, self.skills_allow),
                }
            ]
        )
        messages.append({"role": "user", "content": task})

        iteration = 0
        final_result: str | None = None
        while iteration < self._MAX_ITERATIONS:
            iteration += 1
            if on_delta is None:
                response = await provider.chat_with_retry(
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                )
            else:
                # A spawned run keeps the retry ladder; only a caller that asked
                # to watch the reply form gives it up (a stream that already
                # rendered deltas cannot be retried without duplicating them).
                # The generation settings are passed on so this run answers under
                # the same budget as the same instance's spawns -- chat_stream's
                # signature would otherwise cap it at its own literal 4096.
                response = await stream_llm_call(
                    provider,
                    messages=messages,
                    tools=tools.get_definitions(),
                    model=model,
                    on_token_delta=on_delta,
                    **generation_kwargs(provider),
                )
            # Per iteration, because that is how the cost accrues: this loop calls
            # the model once per round and the run's cost is their sum, unlike an
            # ACP agent's one cumulative report for the whole turn. Both arms
            # land here -- a streamed reply costs the same as a waited-for one.
            activity.note_usage(response.usage)
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
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                    # Before the call, not after: a tool that raises is still
                    # something the sub-agent did, and it is the one a reader
                    # asking "what happened" most needs to see.
                    activity.note_tool_call(tool_call.name)
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
                    if getattr(result, "abort_action", False):
                        raise SubagentActionAbortedError
            else:
                final_result = response.content
                break

        if final_result is None:
            final_result = "Task completed but no final response was generated."
        logger.info("Subagent [{}] completed successfully", task_id)
        if on_messages is not None:
            messages.append({"role": "assistant", "content": final_result})
            on_messages(messages)
        return final_result
