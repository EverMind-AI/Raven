"""Segment 6 + history slot — the Curator, as a SegmentBuilder.

The Curator is just another :class:`SegmentBuilder` (``order=6``,
``needs_prefix=True``). Unlike seg1–5 it produces two things from one
computation: the ``# Curator Working State`` text (system slot, segment
6) and the budget-trimmed ``*history`` (history slot). Both ride out on
a single :class:`Segment` (``text`` + ``history``).

Because it ``needs_prefix``, :class:`ContextAssembler` runs it in phase
B with ``ctx.prefix`` populated (the already-assembled seg1–5 + user +
tools), so its internal budget tools size ``*history`` against the exact
fixed overhead.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from raven.agent.tools.registry import ToolRegistry
from raven.config.raven import ContextConfig
from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.curator import (
    CuratorArchiveMessagesTool,
    CuratorArchiveStore,
    CuratorAssembler,
    CuratorBuildContextTool,
    CuratorCheckBudgetTool,
    CuratorReadMemoryTool,
    CuratorRetrieveArchivedTool,
    CuratorSearchHistoryTool,
    CuratorSetRelevanceTool,
    CuratorState,
    CuratorUpdateWorkingStateTool,
    TurnContext,
    _curator_input_payload,
    _trace_messages,
)
from raven.memory_engine.consolidate.consolidator import MemoryStore
from raven.providers.base import LLMProvider


class CuratorSegmentBuilder:
    """Selects ``*history`` and renders ``# Curator Working State``."""

    name = "curator"
    order = 6
    needs_prefix = True

    def __init__(
        self,
        workspace: Path,
        config: ContextConfig,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        now_fn: Callable[[], datetime] | None = None,
        max_steps: int = 12,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.provider = provider
        self.model = model
        self.curator_model = config.curator_model or model
        self.context_window_tokens = context_window_tokens
        self.get_tool_definitions = get_tool_definitions
        self.max_steps = max_steps
        self.archive = CuratorArchiveStore(workspace, config, now_fn=now_fn)
        self.assembler = CuratorAssembler(
            provider,
            model,
            get_tool_definitions,
            context_window_tokens,
        )
        self._turn_ids: dict[str, str] = {}

    async def build(self, ctx: AssemblyContext) -> Segment | None:
        if ctx.prefix is None:
            raise RuntimeError("CuratorSegmentBuilder requires ctx.prefix (phase B)")

        session_key = ctx.session_key
        turn_id = uuid.uuid4().hex
        self._turn_ids[session_key] = turn_id
        self.assembler.prefix = ctx.prefix

        manifest = self.archive.build_manifest(session_key, ctx.session_messages)
        turn = TurnContext(
            current_message=ctx.current_message,
            media=ctx.media,
            channel=ctx.channel,
            chat_id=ctx.chat_id,
        )
        state = CuratorState(session_key, ctx.session_messages, ctx.budget, turn, manifest)
        self.archive.append_trace(
            session_key,
            turn_id,
            "curator_start",
            {
                "budget": asdict(ctx.budget),
                "message_count": len(ctx.session_messages),
                "max_steps": self.max_steps,
            },
        )

        history_tokens = sum(item.tokens for item in manifest)
        threshold = int(ctx.budget.available_history * self.config.fast_path_threshold)
        if history_tokens < threshold:
            history = self._history_from_messages(ctx.session_messages)
            meta = {
                "path": "fast",
                "history_tokens": history_tokens,
                "threshold_tokens": threshold,
                "trace_path": str(self.archive.trace_path(session_key, turn_id)),
            }
            self.archive.append_trace(session_key, turn_id, "fast_path", meta)
            return Segment(text="", history=history, meta=meta)

        try:
            seg = await self._slow_path(state, turn_id)
            if seg is not None:
                return seg
        except Exception:
            logger.exception("Curator slow path failed; using deterministic fallback")
            self.archive.append_trace(session_key, turn_id, "slow_path_exception", {})

        plan = self.assembler.fallback_plan(state)
        assembled, validation = self.assembler.build(state, plan)
        meta = {
            "path": "fallback",
            "trace_path": str(self.archive.trace_path(session_key, turn_id)),
        }
        self.archive.append_trace(
            session_key,
            turn_id,
            "fallback",
            {
                "plan": asdict(plan),
                "validation": validation,
            },
        )
        return Segment(
            text=self.assembler.working_state_segment(plan.working_state_injection or None),
            history=assembled.messages[1:-1],
            meta=meta,
        )

    async def after_turn(
        self,
        session_key: str,
        response: dict[str, Any],
        usage: dict[str, int] | None = None,
    ) -> None:
        turn_id = self._turn_ids.get(session_key)
        if not turn_id:
            return
        self.archive.append_trace(
            session_key,
            turn_id,
            "main_agent_result",
            {
                "response": response,
                "usage": usage or {},
            },
        )

    # ------------------------------------------------------------------
    # Slow path (bounded internal Curator LLM loop)
    # ------------------------------------------------------------------

    async def _slow_path(self, state: CuratorState, turn_id: str) -> Segment | None:
        registry = self._make_tools(state, turn_id)
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": json.dumps(_curator_input_payload(state, self.archive), ensure_ascii=False)},
        ]
        for step in range(1, self.max_steps + 1):
            self.archive.append_trace(
                state.session_key,
                turn_id,
                "curator_llm_request",
                {
                    "step": step,
                    "messages": _trace_messages(messages),
                    "tools": registry.tool_names,
                },
            )
            response = await self.provider.chat_with_retry(
                messages=messages,
                tools=registry.get_definitions(),
                model=self.curator_model,
                max_tokens=2048,
                temperature=0.1,
            )
            self.archive.append_trace(
                state.session_key,
                turn_id,
                "curator_llm_response",
                {
                    "step": step,
                    "content": response.content,
                    "finish_reason": response.finish_reason,
                    "tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                },
            )
            if response.finish_reason == "error":
                return None
            if not response.has_tool_calls:
                return None

            tool_call_dicts = [tc.to_openai_tool_call() for tc in response.tool_calls]
            messages.append({"role": "assistant", "content": response.content, "tool_calls": tool_call_dicts})
            for tool_call in response.tool_calls:
                result = await registry.execute(tool_call.name, tool_call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": result,
                    }
                )
                self.archive.append_trace(
                    state.session_key,
                    turn_id,
                    "curator_tool_result",
                    {
                        "step": step,
                        "tool": tool_call.name,
                        "arguments": tool_call.arguments,
                        "result": _json_or_text(result),
                    },
                )
                if tool_call.name == "curator_build_context" and state.final_plan is not None:
                    assembled, validation = self.assembler.build(state, state.final_plan)
                    if validation.get("ok"):
                        self.archive.append_trace(
                            state.session_key,
                            turn_id,
                            "slow_path_accepted",
                            {
                                "plan": asdict(state.final_plan),
                                "validation": validation,
                            },
                        )
                        return Segment(
                            text=self.assembler.working_state_segment(state.final_plan.working_state_injection or None),
                            history=assembled.messages[1:-1],
                            meta={
                                "path": "slow",
                                "trace_path": str(self.archive.trace_path(state.session_key, turn_id)),
                                "curator_steps": step,
                            },
                        )
        return None

    def _make_tools(self, state: CuratorState, turn_id: str) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in (
            CuratorCheckBudgetTool(state, self.assembler),
            CuratorArchiveMessagesTool(state, self.archive),
            CuratorRetrieveArchivedTool(self.archive),
            CuratorSearchHistoryTool(state),
            CuratorReadMemoryTool(state, self.archive, MemoryStore(self.workspace)),
            CuratorSetRelevanceTool(state, self.archive),
            CuratorUpdateWorkingStateTool(state, self.archive),
            CuratorBuildContextTool(state, self.assembler),
        ):
            registry.register(tool)
        return registry

    @staticmethod
    def _system_prompt() -> str:
        return """You are Raven Curator, an internal context manager.

Your only job is to build the next main-agent LLM context window.
Never answer the user. Never invent message content. Never call external tools.

Rules:
- Preserve the current user message; Python will add it after your plan.
- Preserve valid tool-call adjacency by selecting related message ids together.
- Prefer recent messages, explicit user constraints, unresolved tasks, decisions, and facts referenced by the current user message.
- Archive old low-relevance messages losslessly before dropping them from live context when useful.
- Retrieve archived content only when needed.
- Finish by calling curator_build_context.
"""

    @staticmethod
    def _history_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
        out: list[dict[str, Any]] = []
        for message in messages:
            entry = {k: v for k, v in message.items() if k in allowed}
            if entry.get("role"):
                out.append(entry)
        for idx, msg in enumerate(out):
            if msg.get("role") == "user":
                return out[idx:]
        return []


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
