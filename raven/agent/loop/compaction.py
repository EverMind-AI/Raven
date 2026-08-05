"""In-turn context compaction for long agentic turns.

A coding turn appends tool results for tens or hundreds of iterations while
the context is assembled only once, so the transcript grows until it hits the
model's real window. This module provides the two shrink tiers the loop uses
both proactively (at a token threshold, before the next call) and reactively
(after a context-overflow error):

- prune: replace the bodies of older tool results with a placeholder. Cheap,
  deterministic, keeps the reasoning chain intact.
- summarize: replace everything between the first user message and a recent
  tail with one LLM-written handoff brief. The tail stays verbatim so the
  model keeps its most recent working state.

The MemoryConsolidator operates at turn boundaries and never runs inside a
turn; this module is the only in-turn mechanism.
"""

from __future__ import annotations

from collections.abc import Callable

PLACEHOLDER = "[earlier tool output elided to fit the context window]"
SUMMARY_MARKER = "[Context summary — earlier steps were compacted to fit the context window]"

# Room the trigger leaves for the model's reply. Mirrors opencode's default:
# the configured reserve, else the provider output budget capped at 20k.
_RESERVED_CAP = 20_000
_RESERVED_FALLBACK = 4_096

# Verbatim tail kept through a summary compaction: 25% of the usable window,
# clamped to [2k, 8k] tokens (opencode's defaults).
_TAIL_CAP = 8_000
_TAIL_FLOOR = 2_000
_TAIL_FRACTION = 0.25

SUMMARY_MAX_TOKENS = 2_000
# Per-message ceiling when rendering the head into the summary request. The
# head already fits the window (we trigger below it), this only guards against
# pathological single messages.
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


def reserved_tokens(configured: int | None, provider_max_tokens: int | None) -> int:
    """Output headroom the trigger keeps free below the window."""
    if configured is not None:
        return configured
    return min(_RESERVED_CAP, provider_max_tokens or _RESERVED_FALLBACK)


def tail_budget(configured: int | None, limit: int, reserved: int) -> int:
    """Token budget for the verbatim tail preserved through a summary."""
    if configured is not None:
        return configured
    usable = max(0, limit - reserved)
    return min(_TAIL_CAP, max(_TAIL_FLOOR, int(usable * _TAIL_FRACTION)))


def projected_context_used(
    last_reported: int,
    messages: list[dict],
    counted_upto: int,
    estimate: Callable[[list[dict]], int],
) -> int:
    """Server-reported context size plus the growth it has not seen yet.

    ``last_reported`` came back with a response; every message appended after
    that (``messages[counted_upto:]`` — this call's tool results) is real
    context the next request will carry but no usage report covers. Ignoring it
    lets the next request overshoot the window by a whole round of tool output.
    """
    if last_reported <= 0 or counted_upto >= len(messages):
        return last_reported
    return last_reported + estimate(messages[counted_upto:])


def should_compact(context_used: int, limit: int, reserved: int) -> bool:
    """True when the last observed context size crosses the trigger line."""
    if limit <= 0:
        return False
    return context_used >= max(0, limit - reserved)


def prune_old_tool_results(messages: list[dict], keep_recent: int) -> tuple[list[dict], int]:
    """Replace the bodies of all but the newest ``keep_recent`` tool results.

    Deterministic, no LLM call. Returns ``(new_messages, num_elided)``;
    ``num_elided == 0`` returns the original list unchanged so callers can
    skip a pointless retry.
    """
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_idxs) <= keep_recent:
        return messages, 0
    elide = set(tool_idxs[:-keep_recent] if keep_recent else tool_idxs)
    pruned: list[dict] = []
    elided = 0
    for i, m in enumerate(messages):
        if i in elide and m.get("content") and m.get("content") != PLACEHOLDER:
            clean = dict(m)
            clean["content"] = PLACEHOLDER
            pruned.append(clean)
            elided += 1
        else:
            pruned.append(m)
    if elided == 0:
        return messages, 0
    return pruned, elided


def prune_old_reasoning(messages: list[dict], keep_recent: int) -> tuple[list[dict], int]:
    """Strip reasoning from all but the newest ``keep_recent`` assistant messages.

    The second accumulator of a long turn: reasoning-heavy models (DSV4 returns
    ``reasoning_content`` inline every turn) can carry 200k+ tokens of past
    reasoning that eliding tool results alone barely dents. Same contract as
    :func:`prune_old_tool_results`.
    """
    idxs = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "assistant" and (m.get("reasoning_content") or m.get("thinking_blocks"))
    ]
    if len(idxs) <= keep_recent:
        return messages, 0
    elide = set(idxs[:-keep_recent] if keep_recent else idxs)
    pruned: list[dict] = []
    elided = 0
    for i, m in enumerate(messages):
        if i in elide:
            clean = dict(m)
            clean.pop("reasoning_content", None)
            clean.pop("thinking_blocks", None)
            pruned.append(clean)
            elided += 1
        else:
            pruned.append(m)
    if elided == 0:
        return messages, 0
    return pruned, elided


def _protected_prefix_end(messages: list[dict]) -> int | None:
    """Index just past the first user message; system prefix and the task
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
    protect_end = _protected_prefix_end(messages)
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
    protect_end = _protected_prefix_end(messages)
    if protect_end is None:
        return messages
    summary_msg = {"role": "user", "content": f"{SUMMARY_MARKER}\n\n{summary}"}
    return [*messages[:protect_end], summary_msg, *messages[split:]]
