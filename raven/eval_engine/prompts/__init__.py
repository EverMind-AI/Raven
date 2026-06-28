"""Prompt templates for the Eval Engine judge / tool audit hooks."""

from raven.eval_engine.prompts.task_completion import TASK_COMPLETION_PROMPT
from raven.eval_engine.prompts.tool_safety import TOOL_SAFETY_PROMPT

__all__ = ["TASK_COMPLETION_PROMPT", "TOOL_SAFETY_PROMPT"]
