"""Pydantic configuration model for the Eval Engine."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class EvalEngineConfig(BaseModel):
    """Eval Engine tunables.

    Default is fully off — instantiating ``EvalEngine`` with a default
    config produces three no-op hooks that don't touch AgentLoop. Operators
    flip ``enabled = True`` and the relevant per-phase toggles to activate
    the engine.
    """

    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    enabled: bool = False
    """Master switch. Off → all three hooks are no-ops."""

    judge_model: str = "claude-haiku-4-5"
    """Cheap small-batch model used for the LLM judge call. ``haiku`` is
    the default since the judge is on a per-turn hot path."""

    judge_timeout_seconds: float = 8.0
    """Hard ceiling on a single judge call. Time-out → judge returns
    ``JudgeVerdict.unknown`` and the hook falls through pass-through."""

    on_task_completion: bool = True
    """When ``enabled``, run the after-iteration judge to write case.md /
    behaviors.md outcomes. Set to False to silence the writer without
    disabling the rest of the engine."""

    on_tool_audit: bool = False
    """Tool audit is an expensive per-tool-call check. Default off so the
    engine ships safe but not loud."""

    on_iteration_gate: bool = False
    """Token-budget / pruning gate that runs before every iteration.
    Default off so the engine has zero overhead in the common case."""

    max_iteration_tokens: int = 40_000
    """If ``on_iteration_gate`` is on, refuse to start another iteration
    once the cumulative messages exceed this token budget."""

    tool_denylist: list[str] = Field(default_factory=list)
    """If ``on_tool_audit`` is on, tool names listed here are blocked
    deterministically before any LLM safety check runs."""


__all__ = ["EvalEngineConfig"]
