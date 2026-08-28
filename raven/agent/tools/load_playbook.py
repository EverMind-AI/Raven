"""``load_playbook`` — the one way into the stored playbook library.

The model decides whether a playbook applies, from this tool's description, the
same way it decides between ``spawn`` and ``run_subagent_dag``. That is a change:
a passive matcher used to decide before the turn began, on one message with no
conversation history, and a hit took the turn over. The decision now sits with
the party that has the context.

**Loading is one action; what it leads to is the playbook's business.** A ``dag``
playbook dispatches from inside this call -- the caller never gets a chance to
"load and then not run", and never sees the graph it would otherwise be tempted to
edit. A ``prompt`` playbook comes back as composition guidance for the caller to
build a graph from. Which of those happens is the author's ``mode``, and it is
deliberately absent from this tool's signature: it describes how thoroughly the
author specified their procedure, which is not something the caller should have to
classify correctly before it can ask.

What the caller may supply is bounded to two arguments -- ``params`` (declared
values) and ``fills`` (fields the author left blank) -- and ``fills`` is refused
for any field the playbook already wrote. So "use this playbook, but change step
3" cannot be expressed, which is the point: the file in git stays an accurate
description of what ran.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.playbook.runtime import PlaybookRuntime


class LoadPlaybookTool(Tool):
    """Load one of the user's stored playbooks and act on it."""

    # A dag playbook dispatches in the background and announces its own result, so
    # the call returns as soon as the graph is accepted.
    timeout_seconds = 120.0

    def __init__(self, runtime: "PlaybookRuntime") -> None:
        self._runtime = runtime
        #: The message this turn is about, for ranking the listing. Set by the
        #: loop before the description is read; empty is fine and yields a stable
        #: alphabetical selection.
        self._turn_message = ""
        self._turn_view: tuple[list[tuple[str, str]], list[str]] | None = None
        self._view_revision = -1

    @property
    def name(self) -> str:
        return "load_playbook"

    def set_turn_message(self, message: str) -> None:
        """Tell the tool what this turn is about, so its listing can be ranked.

        The description is rendered per turn, and which playbooks are worth
        describing depends on the request. Without this the tool would either list
        the whole library on every turn -- fine at five, not at a hundred -- or
        rank against nothing.
        """
        self._turn_message = message or ""
        self._turn_view = None

    def _library_view(self) -> tuple[list[tuple[str, str]], list[str]]:
        if self._turn_view is None or self._view_revision != self._runtime.revision:
            self._turn_view = self._runtime.library_view(self._turn_message)
            # library_view() reconciles the directory and may advance the
            # generation itself, so record it after the read.
            self._view_revision = self._runtime.revision
        return self._turn_view

    def to_schema(self) -> dict[str, Any]:
        """Render one coherent, fresh Playbook view for this LLM iteration."""
        # ToolRegistry calls to_schema() before every LLM call. Clearing here
        # makes edits from other writers visible between iterations, while the
        # lazy cache still gives description and parameters the same snapshot.
        self._turn_view = None
        return super().to_schema()

    @property
    def description(self) -> str:
        listing, names = self._library_view()
        if not listing:
            return "Load a stored playbook. No playbooks are installed."
        lines = "\n".join(f"- {pid}: {detail}" for pid, detail in listing)
        # The full name list only when it is longer than what is described, so the
        # common small-library case does not print everything twice.
        described = {pid for pid, _ in listing}
        rest = [n for n in names if n not in described]
        more = f"\nAlso installed (ask by name for details): {', '.join(rest)}." if rest else ""
        return (
            "Use one of the user's stored playbooks -- a saved multi-step procedure with its own "
            "agents, parameters and steps. Reach for one when the request is the thing a playbook "
            "already describes; a playbook encodes how the user wants this kind of work done, so it "
            "beats improvising the same steps. If none fits, do not force it: use `spawn` or "
            "`run_subagent_dag`, or just do the work.\n"
            "Loading runs it. Most playbooks ship their whole graph, so one call is the whole "
            "interaction; some instead come back with guidance for you to build the graph from, and "
            "some ask for values first. Pass every parameter you can read off the conversation -- "
            "invent nothing -- and `fills` for any field listed below as left for you.\n"
            f"Installed playbooks:\n{lines}{more}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Playbook name, exactly as listed in this tool's description.",
                    # The whole library, not the described subset: a playbook the
                    # per-turn ranking did not surface is still one the user can
                    # name out loud, and an enum that omitted it would make it
                    # unreachable rather than merely undescribed.
                    #
                    # Absent rather than null when there is nothing to offer. An
                    # empty library was unreachable while this tool was withheld
                    # over one, so `or None` was too; it is the fresh-install
                    # state now, and `"enum": null` is not a JSON Schema -- it
                    # also makes every call raise, because `Tool._validate` tests
                    # `val not in schema["enum"]` on the key being present.
                    **({"enum": names} if (names := self._library_view()[1]) else {}),
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Values for the playbook's declared parameters, keyed by its own parameter "
                        "names (see its params list above). Omit what the user has not said -- do "
                        "not invent values."
                    ),
                },
                "fills": {
                    "type": "object",
                    "description": (
                        "Only for fields the playbook lists as left for you: "
                        '{"<node id>": {"promptTemplate": "..."}}. Keyed by the node ids shown '
                        "above. Anything the playbook already specifies is fixed -- filling one is "
                        "refused, so use this to complete a playbook, never to modify one."
                    ),
                },
            },
            "required": ["name"],
        }

    async def execute(
        self,
        name: str,
        params: dict[str, Any] | None = None,
        fills: dict[str, dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        plan = await self._runtime.load(name, params or {}, fills or {})
        if plan is None:
            known = ", ".join(self._runtime.names()) or "(none installed)"
            return f"Error: no playbook named {name!r}. Available: {known}"
        # Every outcome is text for the caller to act on: a dispatch receipt, the
        # guidance to compose from, or what is still missing. The distinction is
        # carried by the words rather than a separate field, because the caller's
        # next move is a reply or a call either way.
        notes = "".join(f"\n[note] {n}" for n in plan.notes)
        return f"{plan.reply}{notes}"
