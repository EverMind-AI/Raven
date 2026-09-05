"""The deck tools re-seated from per-process construction to per-turn address.

The fork built one engine per ACP session (fork ``raven/acp/engine.py``)
because every ppt tool takes its workspace at construction and
``Project.root`` fences one deck inside it -- two sessions on one tool set
would build two decks over each other in one ``deck/``. The trunk host pools
one loop over many concurrent sessions and answers the same problem with the
workdir cargo: ``session/new`` binds a session's directory into its metadata,
every turn body runs inside ``workdir.bind(...)``, and each consumer takes
``workdir.current()`` per call -- the same seam the built-in filesystem, shell
and media tools read (ppt verdict, feature 13).

So the registered face of each deck tool is a :class:`SessionTool`: schema and
name come from a prototype built once at activation, and each call resolves
the bound working directory and forwards to the real fork tool assembled for
that directory. One assembly per working directory, cached for the life of the
process -- the fork's one-engine-per-session shape, minus the AgentLoop it no
longer needs to duplicate; custody follows the fork's own precedent (job
directories were never reclaimed).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from raven.agent import workdir
from raven.contracts.tool import Tool, ToolResult
from raven_ppt.tools import _return


class SessionTool(Tool):
    """One deck tool's registered face: fork schema, per-turn workspace."""

    def __init__(self, prototype: Tool, engine_for: Callable[[Path], Mapping[str, Tool]]) -> None:
        self._prototype = prototype
        self._engine_for = engine_for
        # Instance attributes shadow the ABC's class defaults, so the registry
        # reads the fork tool's own budget off this wrapper.
        self.timeout_seconds = prototype.timeout_seconds
        self.blocking_interaction = prototype.blocking_interaction
        self.channels = prototype.channels

    @property
    def name(self) -> str:
        return self._prototype.name

    @property
    def description(self) -> str:
        return self._prototype.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._prototype.parameters

    async def execute(self, **params: Any) -> str | ToolResult:
        bound = workdir.current()
        if bound is None:
            # Reachable only outside a turn (a direct programmatic call): the
            # loop binds every turn body, whatever the entrance.
            return _return.failed(
                f"{self.name} has no working directory: no turn is bound",
                hint="deck tools run inside an agent turn, whose session directory fences the deck",
            )
        tool = self._engine_for(Path(bound)).get(self.name)
        if tool is None:
            return _return.failed(f"{self.name} is not available on the configured route")
        return await tool.execute(**params)
