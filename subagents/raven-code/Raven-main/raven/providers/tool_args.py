"""Classify and repair tool-call arguments the model wrote as invalid JSON.

Pure decisions only; the loop owns the side effects (same split as
``recovery.py``). Providers call :func:`coerce_arguments` while the raw string
is still in hand, because the flags cannot be recovered once it is parsed.

Two failures look alike and need opposite treatments. A *truncated* object was
cut by the output budget, so its values are silently incomplete -- repairing it
yields parameters that execute but do the wrong thing (a write_file missing its
tail). Retrying with a larger budget is the only fix. A *mangled but closed*
object is a formatting slip; more budget cannot help, but resampling usually
can. Hence both flags, not one.

Truncation is judged from the string's own shape rather than ``finish_reason``:
hermes found gateways that rewrite "length" into "tool_calls", hiding it. The
finish reason is accepted as an extra signal, never as the only one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import json_repair


@dataclass(frozen=True)
class ArgumentFlags:
    """What had to happen to make the arguments usable."""

    repaired: bool = False
    truncated: bool = False


@dataclass(frozen=True)
class ToolArgsLimits:
    """Per-turn budgets for the two recovery ladders."""

    truncation_max_retries: int = 4
    malformed_max_resamples: int = 3
    max_tokens_ceiling: int = 32768

    def next_max_tokens(self, current: int) -> int:
        return min(max(1, current) * 2, self.max_tokens_ceiling)


def limits_from_defaults(defaults: object) -> ToolArgsLimits:
    """Build limits from an ``agents.defaults`` config object (duck-typed)."""
    return ToolArgsLimits(
        truncation_max_retries=getattr(defaults, "tool_args_truncation_max_retries", 4),
        malformed_max_resamples=getattr(defaults, "tool_args_malformed_max_resamples", 3),
        max_tokens_ceiling=getattr(defaults, "tool_args_max_tokens_ceiling", 32768),
    )


def looks_truncated(raw: str) -> bool:
    """True when the string stops before its JSON containers close.

    Scans bracket depth rather than only checking the last character: hermes'
    endswith heuristic passes ``{"todos": [{"content": "x"}``, which closes an
    inner object while the array and outer object are still open. Quote and
    escape state are tracked so brackets inside string values do not count.
    Unescaped quotes in a mangled value can desynchronize that tracking, which
    is acceptable -- such a value is flagged as repaired either way, and
    treating it as closed keeps it off the max_tokens ladder that cannot fix it.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = in_string
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth > 0 or in_string


def coerce_arguments(
    raw: Any,
    *,
    finish_reason: str | None = None,
) -> tuple[dict[str, Any], ArgumentFlags]:
    """Return usable arguments plus what was wrong with them.

    Always yields a dict: ``json_repair`` answers ``''`` for hopeless input and
    ``**''`` raises, which the tool layer would surface as a schema complaint
    that misdirects the model.
    """
    if isinstance(raw, dict):
        return raw, ArgumentFlags(truncated=finish_reason == "length")
    if not isinstance(raw, str) or not raw.strip():
        return {}, ArgumentFlags(truncated=finish_reason == "length")

    truncated = looks_truncated(raw) or finish_reason == "length"
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = json_repair.loads(raw)
        repaired = True
    else:
        repaired = False

    if not isinstance(parsed, dict):
        return {}, ArgumentFlags(repaired=True, truncated=truncated)
    return parsed, ArgumentFlags(repaired=repaired, truncated=truncated)
