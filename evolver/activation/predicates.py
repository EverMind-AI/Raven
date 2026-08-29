"""Single source of truth for mechanism trigger predicates.

Runtime hooks and the evolver's activation-spec evaluators import THESE
functions, so chamber preflight predictions and live hook behavior can
not drift (round-1 incidents C1/C3). Each predicate takes a normalized
record: a dict with 'content' (str), 'tool_calls' (list), optionally
'role'. Hooks normalize live response objects via normalize_response();
the corpus evaluator feeds logged session records directly (same shape).
"""

from __future__ import annotations

import json
import re
from typing import Any

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def normalize_response(response: Any) -> dict:
    if isinstance(response, dict):
        return {"content": str(response.get("content") or ""), "tool_calls": response.get("tool_calls") or []}
    return {
        "content": str(getattr(response, "content", None) or ""),
        "tool_calls": getattr(response, "tool_calls", None) or [],
    }


def is_empty_response(rec: dict) -> bool:
    """Truly dead iteration: no visible content AND no tool calls."""
    return not rec.get("content", "").strip() and not rec.get("tool_calls")


def visible_reasoning_len(rec: dict) -> int:
    """Length of think-stripped, whitespace-collapsed content."""
    stripped = _THINK_RE.sub("", rec.get("content", ""))
    return len(" ".join(stripped.split()))


def is_short_toolcall_iteration(rec: dict, max_chars: int = 80) -> bool:
    """Tool-call iteration whose visible reasoning is below max_chars."""
    return bool(rec.get("tool_calls")) and visible_reasoning_len(rec) < max_chars


def command_head(rec: dict) -> str | None:
    """Head token of the actual shell command (from tool_calls arguments),
    falling back to assistant prose only when no tool call is present.

    The exec-style tool carries the command in tool_calls[0].function.arguments
    (a JSON string or dict) under a 'command' key. Reading the command head
    here (not the prose head) is what makes repeated_failure_run and the
    forced-replan family-detection key on real command families rather than
    on prose openers like 'Let' (C3 round-2 improvement)."""
    tcs = rec.get("tool_calls") or []
    if tcs:
        tc = tcs[0]
        fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
        args = None
        if isinstance(fn, dict):
            args = fn.get("arguments")
        elif fn is not None:
            args = getattr(fn, "arguments", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = None
        if isinstance(args, dict):
            cmd = str(args.get("command") or "").strip()
            if cmd:
                return cmd.split()[0]
    c = rec.get("content", "").strip()
    return c.split()[0] if c else None
