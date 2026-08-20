"""CLI assembly helper for the Eval Engine.

Builds a configured :class:`EvalEngine` ready to be mounted
into AgentLoop's hook chain. Default config has ``enabled=False`` so
this returns a fully-no-op engine that the caller can hand to
AgentLoop without behaviour change — Sentinel-style hooks already in
the chain still take effect.

When the operator flips ``enabled=True`` in config, the same builder
yields the three hooks (before_iteration / tool_audit / after_iteration)
exposed via :meth:`EvalEngine.hooks` for the caller to extend onto
``AgentLoop(hooks=...)`` or a pre-built :class:`CompositeHook`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from raven.eval_engine import EvalEngine, EvalEngineConfig

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider

logger = logging.getLogger(__name__)


def build_eval_stack(
    *,
    provider: "LLMProvider | None" = None,
    memory: "MemoryStore | None" = None,
    config: EvalEngineConfig | None = None,
) -> EvalEngine:
    """Construct an :class:`EvalEngine` from optional dependencies.

    Returns a fully-no-op engine when no config is provided (the
    EvalEngineConfig default is ``enabled=False``). Operators activate
    the engine by passing a config with ``enabled=True`` plus whichever
    per-phase flags they want (on_task_completion / on_tool_audit /
    on_iteration_gate).

    ``provider`` is required only when ``on_task_completion`` is True;
    the engine falls back to a no-op judge otherwise.

    ``memory`` is required only when the after-iteration hook is
    expected to write verdict entries to HISTORY.md.
    """
    engine = EvalEngine(config or EvalEngineConfig(), provider=provider, memory=memory)
    if engine.config.enabled:
        logger.info(
            "Eval Engine: enabled (task_completion=%s tool_audit=%s iteration_gate=%s)",
            engine.config.on_task_completion,
            engine.config.on_tool_audit,
            engine.config.on_iteration_gate,
        )
    return engine


__all__ = ["build_eval_stack"]
