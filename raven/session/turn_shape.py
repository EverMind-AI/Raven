"""What a stored transcript says about the turn it holds.

Two surfaces read the same stored turn and have to answer the same question about
it: the ACP replay, which draws the turn, and the session list, which orders
sessions by it. A turn the message tool answered is not a Silent Turn whatever it
ends on -- the reply reached the reader -- so both keep it. The rule lives here
rather than in either surface: a rule the two copies do not share is a rule that
drifts, and the drift shows as one surface drawing a turn the other hides.

Whether a tool answered is the live turn's verdict -- ``MessageTool.sent_in_turn``, a
send that went out (the tool hands every send to the turn's own reader, whatever chat
the call names), or a file delivery -- filed as ``replied_via_tool`` on the turn's last
assistant row with text or tool calls: the marker's row when the turn ends on one,
which is the only row a reader asks. A reader does not work it out from the calls it
can see: a call that failed answered nobody. The flag has one name in both shapes a
transcript comes in -- the stored row and the flattened row the wire carries -- so a
caller hands over whichever it holds.

Which turns those surfaces take back whole is the same kind of question with the same
answer owed by each, so its rule lives here too (``TakeBack``), and the live turn asks
it before filing a notice after a marker that neither surface will keep.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

REPLIED_VIA_TOOL = "replied_via_tool"
"""The stored and wire name of the flag on a turn's last assistant row with text or tool calls."""


def replied_via_message_tool(row: Mapping[str, Any]) -> bool:
    """Whether this row carries the verdict that a tool put the turn's reply in front of its reader.

    Such a turn has answered, so whatever the model ends it on -- a marker, a cut,
    nothing at all -- the reader has it, and the turn is not a Silent Turn.
    """
    return isinstance(row, Mapping) and row.get("role") == "assistant" and row.get(REPLIED_VIA_TOOL) is True


class TakeBackStep(Enum):
    """What a row means to ``TakeBack``."""

    OPENS = "opens"
    """A user row that opens a turn which can be taken back."""

    TAKES_BACK = "takes_back"
    """The row that closes such a turn on a Silence Marker no tool answered: the turn goes, this row included."""


class TakeBack:
    """Reads a transcript's rows in order and says which turns are taken back whole.

    A turn the runtime opened -- its user row carries ``origin`` -- that the model closed with its
    Silence Marker is taken back whole, the narration and tool calls on the way included: the marker
    is sent as no text, and the runtime's prose for the model would otherwise stand as a message with
    no answer under it. A person's message opens a turn they keep however the model answers it, and a
    turn a tool answered (``replied_via_message_tool``) is no Silent Turn whatever it ends on. The
    marker's row closes the turn either way, so a later row is not read as part of it.

    Rows come in the stored shape, keys without the live turn's underscore.
    """

    def __init__(self) -> None:
        self._open = False

    def read(self, row: Mapping[str, Any]) -> TakeBackStep | None:
        """What ``row``, the next in the transcript, means to the rule; None for a row that means nothing to it."""
        role = row.get("role")
        if role == "user":
            self._open = bool(row.get("origin"))
            return TakeBackStep.OPENS if self._open else None
        if role != "assistant" or row.get("silent") is not True or not self._open:
            return None
        self._open = False
        return None if replied_via_message_tool(row) else TakeBackStep.TAKES_BACK


__all__ = ["REPLIED_VIA_TOOL", "TakeBack", "TakeBackStep", "replied_via_message_tool"]
