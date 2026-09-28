"""Apply task memory projection through the existing native ContextEngine socket."""

from copy import deepcopy
from dataclasses import replace

from raven.context_engine.factory import build_context_engine
from raven.contracts.context import ContextEngine
from raven.utils.tokens import estimate_prompt_tokens

from ...harness.strategies.memory import ContextRequest
from ..observe import plain


def protected_positions(messages, protected):
    positions = []
    start = 0
    for item in protected:
        index = next((i for i in range(start, len(messages)) if messages[i] == item), None)
        if index is None:
            raise ValueError(
                f"memory context removed or changed a protected message with fields {sorted(item)}; preserve the complete mapping including metadata"
            )
        positions.append(index)
        start = index + 1
    return positions


def check_tool_pairs(messages):
    pending = set()
    for message in messages:
        if message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("memory context contains an unsupported message role")
        if message.get("role") == "tool":
            identity = message.get("tool_call_id")
            if identity not in pending:
                raise ValueError("memory context contains an orphan tool result")
            pending.remove(identity)
        else:
            if pending:
                raise ValueError("memory context removed a required tool result")
            calls = message.get("tool_calls") or []
            pending = {call["id"] for call in calls}
            if len(pending) != len(calls):
                raise ValueError("memory context contains duplicate tool call IDs")
    if pending:
        raise ValueError("memory context ends with unanswered tool calls")


class MemoryContext(ContextEngine):
    """Compose after the native assembler; preserve its lifecycle and required prefix.

    The wrapper is supplied through build_runtime.context_engine. Its native
    base is constructed once after the loop has built shared context resources.
    It never replaces the loop's MemoryModule or its mid-iteration shrink path.
    """

    name = "curator-memory"

    def __init__(self, owner, base=None):
        self.owner, self.base = owner, base

    @property
    def owns_compaction(self):
        return self.base.owns_compaction if self.base is not None else True

    def bind(self, runtime, config):
        if self.base is not None:
            return
        loop = runtime.loop
        self.base = build_context_engine(
            workspace=loop.workspace,
            config=loop.context_config,
            builder=loop.context,
            provider=loop.provider,
            model=loop.model,
            context_window_tokens=loop.context_window_tokens,
            get_tool_definitions=loop.tools.get_definitions,
            list_subagents=lambda: loop.subagents.list_agents(),
            get_tool_notices=loop._mcp_tool_notices,
            now_fn=loop._now_fn,
            backend=loop.backend,
            memory_config=loop.memory_config,
            skill_forge_config=config.skill_forge,
            skill_forge_router_config=config.skill_forge.router,
            skill_hub_client=loop._skill_hub_client,
            provider_pool=loop._provider_pool,
            blocklist_reader=loop._skill_blocklist_reader,
        )

    async def assemble(self, session_key, session_messages, budget, *, turn):
        assembled = await self.base.assemble(session_key, session_messages, budget, turn=turn)
        messages = plain(assembled.messages)
        required = [i for i, row in enumerate(messages) if row.get("role") in {"system", "developer"}]
        if messages and len(messages) - 1 not in required:
            required.append(len(messages) - 1)
        protected = [deepcopy(messages[i]) for i in required]
        allowance = max(0, budget.context_length - budget.reserved_output - budget.reserved_tools)
        if estimate_prompt_tokens(protected) > allowance:
            raise ValueError("protected context cannot fit the host token allowance")
        try:
            request = ContextRequest(messages=messages, budget=allowance, required=required)
            result = await self.owner.call("compose", request, source="assembly", readonly=True)
            positions = protected_positions(result.messages, protected)
            if estimate_prompt_tokens(result.messages) > allowance:
                request = ContextRequest(messages=result.messages, budget=allowance, required=positions)
                result = await self.owner.call("compact", request, source="assembly", readonly=True)
            protected_positions(result.messages, protected)
            check_tool_pairs(result.messages)
            size = estimate_prompt_tokens(result.messages)
            if size > allowance:
                raise ValueError(f"memory context exceeds host token allowance: {size} > {allowance}")
        except Exception as exc:
            self.owner.recorder.add("memory.error", operation="assembly", error=f"{type(exc).__name__}: {exc}")
            raise
        self.owner.recorder.add(
            "memory.context", estimated_tokens=size, allowance=allowance, compacted=result.messages != messages
        )
        return replace(
            assembled,
            messages=result.messages,
            include_indices=None,
            metadata={**assembled.metadata, "curator_memory_tokens": size},
        )

    async def after_turn(self, session_key, outcome):
        await self.base.after_turn(session_key, outcome)

    def set_provider(self, provider, model):
        if self.base is not None:
            self.base.set_provider(provider, model)

    def set_context_window(self, tokens):
        if self.base is not None:
            self.base.set_context_window(tokens)
