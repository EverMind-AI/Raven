"""In-turn transcript compaction helpers for long agentic turns.

A tool-heavy turn appends results for tens of iterations while the context is
assembled only once, so the working transcript grows until it hits the model's
window. The loop has always owned the reactive half of that problem: on a
provider's overflow error it elides older tool-result bodies and retries
(``_emergency_shrink``). This module carries the pure pieces of the other
half, used by the loop only when ``agents.defaults.compaction.enabled`` is on:

- trigger arithmetic: when the last observed context size crosses the line
  (``should_compact`` / ``reserved_tokens``), act before the next call instead
  of waiting for the overflow;
- head summary: replace everything between the first user message and a
  recent verbatim tail with one LLM-written handoff brief (``select_split`` /
  ``render_transcript`` / ``build_compacted``); the tail budget comes from
  ``tail_budget``.

The MemoryConsolidator operates at turn boundaries and never runs inside a
turn; this module plus ``_emergency_shrink`` are the only in-turn mechanisms.
"""

from __future__ import annotations

from collections.abc import Callable

SUMMARY_MARKER = "[Context summary — earlier steps were compacted to fit the context window]"

# Room the trigger leaves for the model's reply: the configured reserve, else
# the resolved output budget capped at 20k.
_RESERVED_CAP = 20_000

# Verbatim tail kept through a summary compaction: 25% of the usable window,
# clamped to [2k, 8k] tokens.
_TAIL_CAP = 8_000
_TAIL_FLOOR = 2_000
_TAIL_FRACTION = 0.25

SUMMARY_MAX_TOKENS = 2_000
# Per-message ceiling when rendering the head into the summary request. The
# head already fits the window (the trigger fires below it), this only guards
# against pathological single messages.
_TRANSCRIPT_MSG_CAP = 8_000

SUMMARY_INSTRUCTIONS = (
    "You are compacting an agent's working context. Summarize the transcript "
    "below into a handoff brief for the same agent to continue the task. "
    "Preserve, with exact names, paths and values:\n"
    "1. The task goal and any acceptance criteria or output contract.\n"
    "2. Confirmed facts about the code and environment (files read, entry "
    "points, interfaces, data formats).\n"
    "3. What has been done so far: files created or edited and how, commands "
    "run and their outcomes.\n"
    "4. Test or verification results, quoting failing output verbatim when "
    "short.\n"
    "5. What remains to do, the current plan, and unresolved errors or open "
    "questions.\n"
    "Be dense and factual. Do not invent anything that is not in the "
    "transcript."
)


def reserved_tokens(configured: int | None, max_output_tokens: int) -> int:
    """Output headroom the trigger keeps free below the window."""
    if configured is not None:
        return configured
    return min(_RESERVED_CAP, max_output_tokens)


def tail_budget(configured: int | None, limit: int, reserved: int) -> int:
    """Token budget for the verbatim tail preserved through a summary."""
    if configured is not None:
        return configured
    usable = max(0, limit - reserved)
    return min(_TAIL_CAP, max(_TAIL_FLOOR, int(usable * _TAIL_FRACTION)))


def should_compact(context_used: int, limit: int, reserved: int, trigger_ratio: float | None = None) -> bool:
    """True when the last observed context size crosses the trigger line.

    The base trigger is ``limit - reserved`` (just below overflow). A
    ``trigger_ratio`` in (0, 1) lowers the line to ``trigger_ratio * limit``
    when that is smaller, so compaction fires proactively instead of at the
    brink.
    """
    if limit <= 0:
        return False
    threshold = max(0, limit - reserved)
    if trigger_ratio is not None and 0 < trigger_ratio < 1:
        threshold = min(threshold, int(trigger_ratio * limit))
    return context_used >= threshold


def protected_prefix_end(messages: list[dict]) -> int | None:
    """Index just past the first user message; the system prefix and the task
    statement never enter the summarized head."""
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            return i + 1
    return None


def select_split(
    messages: list[dict],
    budget: int,
    estimate: Callable[[list[dict]], int],
) -> int | None:
    """Pick where the verbatim tail starts.

    Walks backward from the end accumulating estimated tokens until ``budget``
    is spent (the last message is always kept), then backs up so the tail
    never opens on a ``role=="tool"`` message — that would orphan it from the
    assistant tool_call it answers. Returns ``None`` when the remaining head
    is too small to be worth a summary call.
    """
    protect_end = protected_prefix_end(messages)
    if protect_end is None:
        return None
    total = 0
    split = len(messages)
    for i in range(len(messages) - 1, protect_end - 1, -1):
        cost = estimate([messages[i]])
        if split < len(messages) and total + cost > budget:
            break
        total += cost
        split = i
    while protect_end < split < len(messages) and messages[split].get("role") == "tool":
        split -= 1
    if split - protect_end < 2:
        return None
    return split


def render_transcript(messages: list[dict]) -> str:
    """Flatten the head into role-labeled text for the summary request."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        parts: list[str] = []
        content = m.get("content")
        if content:
            text = str(content)
            if len(text) > _TRANSCRIPT_MSG_CAP:
                text = text[:_TRANSCRIPT_MSG_CAP] + " …[truncated]"
            parts.append(text)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or (tc.get("name") if isinstance(tc, dict) else "")
            args = fn.get("arguments") or ""
            parts.append(f"→ called {name}({str(args)[:300]})")
        if parts:
            lines.append(f"[{role}] " + "\n".join(parts))
    return "\n\n".join(lines)


def build_compacted(messages: list[dict], split: int, summary: str) -> list[dict]:
    """Rebuild the transcript as [protected prefix, summary, verbatim tail]."""
    protect_end = protected_prefix_end(messages)
    if protect_end is None:
        return messages
    summary_msg = {"role": "user", "content": f"{SUMMARY_MARKER}\n\n{summary}"}
    return [*messages[:protect_end], summary_msg, *messages[split:]]
