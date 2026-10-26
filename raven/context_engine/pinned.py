"""Pinned skill bodies -- which history messages carry one.

A skill body arrives as a ``use_skill`` / ``read_skill`` tool result, which is
an ordinary history message and is dropped like any other old one. That is
usually fine -- re-fetch it -- and silently wrong for instruction material
whose tool description says "unless the guide is already in your context": the
agent cannot observe whether it still is, so it either re-fetches every turn or
builds from the memory of a body that is gone. ``context.pinnedSkillIds`` names
the bodies that must survive; this module answers which message indices that
is, for a given history.

Shared rather than owned by one history strategy: the config field is
strategy-independent and every strategy that drops history has to know what it
may not drop. What each does with the answer differs -- the Curator raises
their manifest relevance, the compacting strategy holds them out of the range
it compacts -- but the question is one question, and it is answered with the
same positional tool pairing ``HistoryTrimmer`` drops by.
"""

from __future__ import annotations

import json
from typing import Any

from raven.context_engine.history_trimmer import _tool_parents

# Tools whose result is a skill body rather than conversation. ``read_skill``
# is included because it returns the same body for a local skill, so which of
# the two the agent happened to call must not decide whether the pin holds.
_SKILL_FETCH_TOOLS = frozenset({"use_skill", "read_skill"})


def _tool_call_name_args(tool_call: Any) -> tuple[str, dict[str, Any]]:
    """``(name, arguments)`` from one stored tool_call, or ``("", {})``.

    Arguments are serialized as a JSON *string* on the way into the session
    (OpenAI shape, see ``providers.tool_calls.openai_tool_call``) but arrive as a dict
    from some providers, so both are accepted. Anything unparseable degrades to
    "no arguments", never an exception -- this runs inside context assembly,
    where a raise would fail the whole turn.
    """
    if not isinstance(tool_call, dict):
        return ("", {})
    fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else tool_call
    name = fn.get("name") if isinstance(fn.get("name"), str) else ""
    raw = fn.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    return (name or "", raw if isinstance(raw, dict) else {})


def pinned_message_ids(messages: list[dict[str, Any]], skill_ids: list[str] | None) -> set[int]:
    """Ids of the messages that fetched a pinned skill's body.

    Returns the assistant message carrying the fetch and every result paired
    with it by ``_tool_parents``, including sibling results. This is the same
    positional pairing used by ``HistoryTrimmer.tool_group``: the trimmer drops
    whole tool groups and treats a group with any protected member as protected.

    Only the latest fetch per skill id is pinned: a re-read supersedes the
    earlier copy, so the same 2k-token body can never occupy the window twice.
    """
    wanted = {s for s in (skill_ids or []) if isinstance(s, str) and s}
    if not wanted:
        return set()

    latest: dict[str, int] = {}
    for idx, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        for tool_call in message.get("tool_calls") or []:
            name, args = _tool_call_name_args(tool_call)
            if name not in _SKILL_FETCH_TOOLS:
                continue
            skill_id = args.get("skill_id")
            if isinstance(skill_id, str) and skill_id in wanted:
                latest[skill_id] = idx

    parents = set(latest.values())
    pinned = parents | {result for result, parent in _tool_parents(messages).items() if parent in parents}
    return pinned


__all__ = ["pinned_message_ids"]
