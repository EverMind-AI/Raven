"""What a stored transcript says about the turn it holds.

Two surfaces read the same stored turn and have to answer the same question about
it: the ACP replay, which draws the turn, and the session list, which orders
sessions by it. A turn the message tool answered is not a Silent Turn whatever it
ends on -- the reply reached the reader -- so both keep it. The rule lives here
rather than in either surface: a rule the two copies do not share is a rule that
drifts, and the drift shows as one surface drawing a turn the other hides.

Both shapes a transcript comes in are read here. A stored row keeps the
provider's call shape (``{"id", "type", "function": {"name", "arguments"}}``) and
the wire carries the flattened one (``{"id", "name", "arguments"}``), so a caller
hands over whichever it holds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

MESSAGE_TOOL = "message"
"""The tool that delivers a reply to the reader, named as the registry names it."""


def calls_message_tool(row: Mapping[str, Any]) -> bool:
    """Whether this row called the ``message`` tool.

    Only an assistant row calls tools; a row that does is not asked.
    """
    if not isinstance(row, Mapping) or row.get("role") != "assistant":
        return False
    for call in row.get("tool_calls") or ():
        if _call_name(call) == MESSAGE_TOOL:
            return True
    return False


def turn_called_message_tool(rows: Iterable[Mapping[str, Any]]) -> bool:
    """Whether any of these rows called the ``message`` tool.

    A turn that called it has answered: the tool sent the reply, so whatever the
    model ends the turn on -- a marker, a cut, nothing at all -- the reader has
    it, and the turn is not a Silent Turn.
    """
    return any(calls_message_tool(row) for row in rows)


def _call_name(call: Any) -> str:
    """The tool a call names, whichever shape it arrives in."""
    if not isinstance(call, Mapping):
        return ""
    name = call.get("name")
    if isinstance(name, str) and name:
        return name
    fn = call.get("function")
    if isinstance(fn, Mapping):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    return ""


__all__ = ["MESSAGE_TOOL", "calls_message_tool", "turn_called_message_tool"]
