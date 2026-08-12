"""The default subagent backend: a bounded in-process Raven agent loop.

This is the logic that used to live inline in ``SubagentManager._run_subagent_inner``
and ``_build_subagent_prompt``, extracted verbatim so a spawned sub-agent's
*executor* becomes pluggable without changing default behavior.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

from loguru import logger

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


def build_subagent_prompt(agent_home: Path, work_dir: Path, tool_names: Collection[str] = ()) -> str:
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
    skills_summary = catalog.build_skills_summary(only=visible) if visible else ""
    if skills_summary:
        parts.append(f"## Skills\n\nRead SKILL.md with read_file to use a skill.\n\n{skills_summary}")

    return "\n\n".join(parts)


class RavenLoopBackend:
    """Runs the task as a bounded in-process Raven agent loop (the default)."""

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

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
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
    ) -> str:
        # Build subagent tools (no message tool, no spawn tool).
        tools = ToolRegistry()
        # Two roots, matching the main loop: the session directory the run works
        # in, and agent home, whose absolute paths this prompt hands out.
        allowed_dirs = (workspace, self.agent_home) if self.restrict_to_workspace else ()
        # follow_binding=False: this run is a background asyncio task that can
        # outlive the turn that spawned it, since SubagentManager.spawn captures
        # the workspace at spawn time, so its tools must fence on the directory
        # captured for this run, not on whatever the ambient workdir binding
        # holds by the time they actually execute.
        tools.register(ReadFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        tools.register(WriteFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        tools.register(EditFileTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
        tools.register(ListDirTool(workspace=workspace, allowed_dirs=allowed_dirs, follow_binding=False))
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
        tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        tools.register(WebFetchTool(api_key=self.jina_api_key, proxy=self.web_proxy))

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_subagent_prompt(self.agent_home, workspace, tools.tool_names)},
            {"role": "user", "content": task},
        ]

        iteration = 0
        final_result: str | None = None
        while iteration < self._MAX_ITERATIONS:
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
                for tool_call in response.tool_calls:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
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
        return final_result
