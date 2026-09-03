"""Where a suspended node waits, and where the main agent's answer lands.

One desk per run, held by the graph tool beside that run's cancel event, so the
`resolve_dag_node` control tool can reach a node the runner is waiting on. In
memory only: a gateway restart drops every pending adjudication, and those nodes
read back `interrupted` -- the same outcome an in-flight run already has when the
process dies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

CONTINUE = "continue"
ABANDON = "abandon"
DECISIONS = (CONTINUE, ABANDON)


@dataclass(frozen=True)
class Adjudication:
    """What the main agent decided about one suspended node."""

    decision: str
    message: str | None = None


class AdjudicationDesk:
    """The pending adjudications of one run."""

    def __init__(self) -> None:
        self._waiting: dict[str, asyncio.Event] = {}
        self._answers: dict[str, Adjudication] = {}
        self._reports: dict[str, str] = {}

    def open(self, node_id: str, report: str = "") -> asyncio.Event:
        """Start waiting on ``node_id``. The event fires when an answer lands.

        ``report`` is kept for whoever does the asking. The model's lane does not
        need it -- it was already sent the report when the node suspended -- but
        the foreground lane asks a person from the wave loop, long after the node
        that built the report has returned and released its slot.
        """
        event = asyncio.Event()
        self._waiting[node_id] = event
        if report:
            self._reports[node_id] = report
        return event

    def report(self, node_id: str) -> str:
        """What this node was suspended for, or "" if it was opened without one."""
        return self._reports.get(node_id, "")

    def is_open(self, node_id: str) -> bool:
        return node_id in self._waiting

    def waiter(self, node_id: str) -> asyncio.Event:
        """The event for ``node_id``, opening one if it is not already open."""
        event = self._waiting.get(node_id)
        if event is None:
            event = self.open(node_id)
        return event

    def open_nodes(self) -> set[str]:
        return set(self._waiting)

    def resolve(self, node_id: str, decision: str, message: str | None) -> bool:
        """Record an answer and wake the waiter. False when nobody was waiting.

        The caller reports that False to the model rather than swallowing it: by
        the time an answer arrives the node may have timed out or the run may
        have been cancelled, and a silently discarded decision looks to the model
        exactly like one that was applied.
        """
        event = self._waiting.get(node_id)
        if event is None:
            return False
        self._answers[node_id] = Adjudication(decision=decision, message=message)
        event.set()
        return True

    def take(self, node_id: str) -> Adjudication | None:
        """The answer for ``node_id``, consumed. ``None`` if none landed."""
        self._waiting.pop(node_id, None)
        self._reports.pop(node_id, None)
        return self._answers.pop(node_id, None)

    def close(self, node_id: str) -> None:
        """Stop waiting on ``node_id`` without consuming an answer."""
        self._waiting.pop(node_id, None)
        self._answers.pop(node_id, None)
        self._reports.pop(node_id, None)
