"""The types a picked-up run is reported with, apart from the code that picks one up.

``PickedUp``, ``UnreachableError`` and ``PICKED_UP_NOTE`` are what every caller of
a pick-up pass needs: the two entries that pick a run up (``remote_resume`` for an
acp run, ``raven.agent.subagent.backends.cli_agent.CliAgentBackend.pick_up`` for a
cli one) and ``resume_remote``, which routes between them. They live in this leaf so
that the cli backend does not have to import ``remote_resume``: that import drew the
cli backend and the pick-up module into one cycle (``cli_agent -> remote_resume ->
acp_agent -> backends -> cli_agent``), and the guard in
``tests/test_import_cycle_budget.py`` counts a function-local import exactly like a
top-level one. Nothing here imports anything of raven's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: What the reply of a picked-up turn ends with: the reader learns it crossed a restart.
PICKED_UP_NOTE = "picked up after this computer restarted"


class UnreachableError(RuntimeError):
    """The machine could not be asked: the record stays, for a later start."""


@dataclass(frozen=True)
class PickedUp:
    status: str
    """``ok`` with the reply, or ``error`` with the sentence that says why there is none."""
    reply: str
    stop_reason: Any = None
