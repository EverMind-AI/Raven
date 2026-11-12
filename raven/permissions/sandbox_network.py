"""What a tool does with the connections a sandbox's network policy refused while it ran.

A sandbox refuses a connection at run time, after the gate has already let the
command through, so the refusal never passes the gate. This module is where it
meets the permission layer: it names the refused targets to the model, which
otherwise sees only a connection error, and records them on the turn, so a run
nobody watched still reports what it could not reach.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from raven.permissions.turn import note_refusal
from raven.sandbox.denials import NetworkDenial, denial_line

if TYPE_CHECKING:
    from raven.sandbox.interfaces import SandboxExecutor

REFUSAL_SOURCE = "sandbox_network"
# A command can be refused hundreds of hosts in one run (a script looping over
# a list); the output it appends to is already capped, so this notice is too.
_MAX_LISTED = 10
_MAX_LINE = 160


async def handle_denials(executor: SandboxExecutor | None, denials: Sequence[NetworkDenial], *, command: str) -> str:
    """The text to append to the tool's output for ``denials``; empty when there are none."""
    if not denials:
        return ""
    lines = [_shortened(denial_line(denial)) for denial in denials[:_MAX_LISTED]]
    for line in lines:
        note_refusal("exec", line, "blocked by the sandbox network policy", REFUSAL_SOURCE)
    rest = len(denials) - len(lines)
    if rest:
        note_refusal(
            "exec", f"and {rest} more blocked targets", "blocked by the sandbox network policy", REFUSAL_SOURCE
        )
    listed = ", ".join(lines) + (f", and {rest} more" if rest else "")
    return (
        f"\n\n[sandbox] The sandbox network policy blocked: {listed}. "
        "This is a policy block, not a network failure. Tell the user; it can be allowed under permissions.network."
    )


def _shortened(line: str) -> str:
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 3] + "..."
