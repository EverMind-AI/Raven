"""What a customer on an online chat channel receives from one employee turn.

A channel shows only finished output (`raven/gateway/outlet.py`): the text replies, each file delivery with the
caption the employee wrote for it, and the short notice that an answer came without an organ. Progress lines, tool
hints and reasoning are never sent to a channel, so they are not part of what the customer, the owner or the record
treats as said to the customer.
"""

from pathlib import Path

DEGRADED = "organ_degraded"


def seen(records, delivered=()) -> str:
    """The turn's customer-facing text in order; a delivery's caption counts once one of its files was kept."""
    kept = {Path(str(path)).name for path in delivered}
    parts = []
    for row in records:
        if row.get("kind") != "runner.event":
            continue
        event = row.get("event") or {}
        if row.get("event_type") == "Text":
            parts.append(str(event.get("content") or ""))
        elif row.get("event_type") == "Notice" and str(event.get("kind") or "").lower() == DEGRADED:
            parts.append(str(event.get("detail") or ""))
        elif event.get("name") == "deliver_files" and event.get("phase") == "start":
            arguments = event.get("arguments") or {}
            files = {Path(str(item.get("path") or "")).name for item in arguments.get("files") or []}
            if files & kept:
                parts.append(str(arguments.get("message") or ""))
    return "\n".join(part for part in parts if part.strip())


def said(exchange) -> str:
    """What the customer received in reply to one message."""
    return seen(exchange.execution.records, exchange.execution.deliverables)
