"""What a stored transcript says about the turn it holds.

Two surfaces read the same stored turn and have to answer the same question about
it: the ACP replay, which draws the turn, and the session list, which orders
sessions by it. A turn the message tool answered is not a Silent Turn whatever it
ends on -- the reply reached the reader -- so both keep it. The rule lives here
rather than in either surface: a rule the two copies do not share is a rule that
drifts, and the drift shows as one surface drawing a turn the other hides.

Whether the message tool answered is the live turn's verdict
(``MessageTool.sent_in_turn``: a send to the turn's own chat that went out), filed
as ``replied_via_tool`` on the turn's last assistant row with text or tool calls -- the
marker's row when the turn ends on one, which is the only row a reader asks. A reader does not work it
out from the calls it can see: a call that failed, or that sent to another chat,
answered nobody. The flag has one name in both shapes a transcript comes in -- the
stored row and the flattened row the wire carries -- so a caller hands over
whichever it holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REPLIED_VIA_TOOL = "replied_via_tool"
"""The stored and wire name of the flag on a turn's last assistant row with text or tool calls."""


def replied_via_message_tool(row: Mapping[str, Any]) -> bool:
    """Whether this row carries the verdict that the message tool sent its turn's reply to the turn's own chat.

    Such a turn has answered, so whatever the model ends it on -- a marker, a cut,
    nothing at all -- the reader has it, and the turn is not a Silent Turn.
    """
    return isinstance(row, Mapping) and row.get("role") == "assistant" and row.get(REPLIED_VIA_TOOL) is True


__all__ = ["REPLIED_VIA_TOOL", "replied_via_message_tool"]
