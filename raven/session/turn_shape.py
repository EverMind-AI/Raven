"""What a stored transcript says about the turn it holds.

Two surfaces read the same stored turn and have to answer the same question about
it: the ACP replay, which draws the turn, and the session list, which orders
sessions by it. A turn the message tool answered is not a Silent Turn whatever it
ends on -- the reply reached the reader -- so both keep it. The rule lives here
rather than in either surface: a rule the two copies do not share is a rule that
drifts, and the drift shows as one surface drawing a turn the other hides.

Whether the message tool answered is the live turn's verdict
(``MessageTool.sent_in_turn``: a send to the turn's own chat that went out), filed
on the row that closes the turn as ``replied_via_tool``. A reader does not work it
out from the calls it can see: a call that failed, or that sent to another chat,
answered nobody. The flag has one name in both shapes a transcript comes in -- the
stored row and the flattened row the wire carries -- so a caller hands over
whichever it holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REPLIED_VIA_TOOL = "replied_via_tool"
"""The stored and wire name of the flag on a turn's closing row."""


def replied_via_message_tool(row: Mapping[str, Any]) -> bool:
    """Whether this row closes a turn whose reply the message tool sent to the turn's own chat.

    Such a turn has answered, so whatever the model ends it on -- a marker, a cut,
    nothing at all -- the reader has it, and the turn is not a Silent Turn.
    """
    return isinstance(row, Mapping) and row.get("role") == "assistant" and row.get(REPLIED_VIA_TOOL) is True


__all__ = ["REPLIED_VIA_TOOL", "replied_via_message_tool"]
