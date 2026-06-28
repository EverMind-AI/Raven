"""Discovery prompt — generates 3-4 candidate task suggestions per day.

This is intentionally separate from the reactive Planner prompt
(``raven.proactive_engine.sentinel.trigger_policy.prompts``) because the output shape is different:

- Planner answers a single ``should-I-nudge / spawn / skip`` decision.
- Discoverer produces a *list* of options the user picks from later.

We expose both shapes via JSON tool-call. Keep the prompt schematic and
short so cheap models (qwen3.5-27B) can reliably emit valid JSON.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Tool-call schema for the LLM. ``options`` is the only data path —
# free-form text is not consumed downstream.
_DISCOVERY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_task_options",
        "description": (
            "Emit 3 to N task-suggestion options the user might want to do "
            "today. Skip the call entirely (no tool invocation) if no "
            "high-value option exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": (
                                    "Short user-facing title (≤ 30 chars). "
                                    "Imperative form: '草拟回复 X' / 'Draft "
                                    "reply to X'. No trailing punctuation."
                                ),
                            },
                            "why": {
                                "type": "string",
                                "description": (
                                    "1-line rationale tied to the user's "
                                    "actual context (≤ 60 chars). 'Noticed "
                                    "X messaged you yesterday and you "
                                    "haven't replied' beats 'might be "
                                    "useful'."
                                ),
                            },
                            "type": {
                                "type": "string",
                                "enum": ["routine_confirm", "ad_hoc"],
                                "description": (
                                    "routine_confirm = upgrade an existing "
                                    "candidate Routine to active "
                                    "(reuse the routine_id). ad_hoc = a "
                                    "fresh one-off task."
                                ),
                            },
                            "exec_kind": {
                                "type": "string",
                                "enum": ["reply", "tool", "spawn",
                                         "routine_confirm"],
                                "description": (
                                    "How ActionExecutor should run the "
                                    "option if the user picks it: 'reply' "
                                    "→ inject a user-side prompt for the "
                                    "agent; 'tool' → call a tool "
                                    "directly; 'spawn' → SubagentManager; "
                                    "'routine_confirm' → upgrade a "
                                    "candidate Routine."
                                ),
                            },
                            "exec_payload": {
                                "type": "object",
                                "description": (
                                    "Kind-specific payload. For 'reply': "
                                    "{prompt}. For 'tool': {tool, args}. "
                                    "For 'spawn': {task_description, "
                                    "max_iterations}. For "
                                    "'routine_confirm': {routine_id, "
                                    "make_cron, cron_expr}."
                                ),
                            },
                            "source": {
                                "type": "string",
                                "enum": ["history", "memory", "routine"],
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "medium", "high"],
                            },
                            "deadline": {
                                "type": "string",
                                "description": (
                                    "ISO date YYYY-MM-DD if the task has "
                                    "an explicit due date in the user's "
                                    "context. Empty string if none. Copy "
                                    "the date from context — do not invent."
                                ),
                            },
                        },
                        "required": ["title", "why", "type", "exec_kind",
                                     "exec_payload"],
                    },
                }
            },
            "required": ["options"],
        },
    },
}


def build_discovery_prompt(
    *,
    now: datetime,
    memory_md: str,
    history_recent: str,
    sentinel_observations_block: str,
    candidate_routines_summary: str,
    fire_history_summary: str,
    max_options: int,
) -> list[dict[str, Any]]:
    """Build chat-style messages for the discovery LLM call.

    Inputs are already-rendered strings; caller owns token budget — we
    do not truncate."""

    system = (
        "You are Raven's daily task-discovery assistant. Once a day "
        "you look at the user's recent activity and propose 3-4 "
        "actionable options they could do today. The user will pick from "
        "your list (or skip), so each option must be:\n"
        "  - **specific** — a concrete action, not a generic suggestion\n"
        "  - **grounded** — referenced in their recent history or memory\n"
        "  - **actionable** — they can confirm and act today\n"
        "\n"
        "Return options via the emit_task_options tool. Skip the call "
        "entirely (no tool invocation, empty response) if you cannot "
        "find 3 high-value options.\n"
        "\n"
        "**Drill-down preference (important):** If the user has a single "
        "complex task they've explicitly deferred (mentioned in history "
        "but not started — e.g. 'optimize X', 'refactor Y', 'research Z'), "
        "PREFER generating 2-3 sibling options that are different "
        "APPROACHES to that one task (varied effort/risk/permanence) "
        "OVER spreading the slots across 3 unrelated tasks. The user is "
        "much more likely to act on a deferred task when they can pick "
        "ONE entry-point from a menu of pre-decomposed approaches than "
        "when they have to remember it AND figure out where to start.\n"
        "Example shape: ['加索引 (5 min)', '重构 N+1 (30 min)', "
        "'加缓存层 (1 h)'] — three approaches to the SAME deferred "
        "optimization, with effort estimates so the user picks by "
        "available time.\n"
        "Reserve the last 1-2 slots for unrelated 'continue X' options "
        "only when there's also active work to surface.\n"
        "\n"
        "Hard rules (violations break downstream routing):\n"
        "  - DO NOT propose anything you've already nudged about in the "
        "last 24h (see the Recent fires section).\n"
        "  - DO NOT propose during the user's quiet hours window.\n"
        "  - For routine_confirm options, use the existing routine's id "
        "verbatim — DO NOT invent new ids.\n"
        "  - exec_payload schema must match exec_kind exactly (see tool "
        "spec).\n"
        "  - If a task has an explicit due date in context, set `deadline` "
        "to its ISO date (YYYY-MM-DD). Still surface tasks whose deadline "
        "has already passed relative to the Discovery context date (the "
        "user may have let it slip) — do NOT drop them; the menu marks them "
        "overdue automatically.\n"
        "  - For 'reply' or 'spawn' options pointing at a deferred task, "
        "the exec_payload.prompt / task_description must be CONCRETE "
        "enough for the agent to act immediately (cite specific file "
        "paths, function names, schema details from MEMORY.md when "
        "available — not 'do X' but 'draft ALTER TABLE on reports.foo "
        "to add (col_a, col_b) index, ...').\n"
    )

    user = (
        f"# Discovery context — {now.strftime('%Y-%m-%d %A %H:%M')}\n"
        "\n"
        "## Long-term memory (MEMORY.md)\n"
        f"{memory_md or '(empty)'}\n"
        "\n"
        "## Recent history (last 24h, HISTORY.md tail)\n"
        f"{history_recent or '(empty)'}\n"
        "\n"
        "## Sentinel observations (auto, last 7 days)\n"
        f"{sentinel_observations_block or '(no observations yet)'}\n"
        "\n"
        "## Candidate routines (from RoutineLearner)\n"
        f"{candidate_routines_summary or '(none detected)'}\n"
        "\n"
        "## Recent fire history (don't repeat these)\n"
        f"{fire_history_summary or '(none)'}\n"
        "\n"
        f"Generate up to {max_options} options. Quality over quantity — "
        "fewer options is better than padding with weak ones."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def discovery_tool_schema() -> list[dict[str, Any]]:
    """Return the tool-schema list to pass to ``provider.chat_with_retry``."""
    return [_DISCOVERY_TOOL_SCHEMA]


__all__ = ["build_discovery_prompt", "discovery_tool_schema"]
