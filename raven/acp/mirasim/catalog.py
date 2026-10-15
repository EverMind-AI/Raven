"""What one Mirasim agent offers, read from ``ui-cli agents`` and ``ui-cli catalog``.

Read once when the adapter starts and again when the copy is old, never per
turn: each read starts a Mirasim server of its own, measured at five to seven
seconds. The menus are advertised in the shapes Raven already reads off any ACP
agent -- the model as the ``session/new`` config option whose ``category`` is
``model``, and the effort rungs as session modes -- so the model picker, a
per-instance model and the session tier reach a Mirasim agent unchanged.

Three kinds of model share one catalog, told apart by ``routeByModel``: a row
routed ``relay`` runs on the Mirasim platform's quota, one routed ``direct`` on
a key the user added to Mirasim (its id is ``profile:<uuid>``), and an unrouted
row is the agent's own, served by its own sign-in. The menu groups them under
those three headings, because the same model can sit in two of them -- a
platform ``deepseek-flash`` and an own-key profile that also reaches DeepSeek --
and the label alone cannot say which one bills where.

Which of the two a session bills, the platform's quota or the user's own
account, is the session's route, not a property of the model id: Mirasim puts a
session on one with ``--route`` on its first turn, or with ``session route``
after, and the id it runs stays the same. The platform runs the own models of the
agents in :data:`PLATFORM_ROUTE_AGENTS` too, so for those each own model is
offered once per route, the route prefixed to its id -- ``own:`` under the
agent's heading, ``platform:`` under the platform's. The bare id is still taken,
as the model on whichever route Mirasim picks: it is what a session reports with
nothing chosen, and so what Raven sends back to undo a choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

MODEL_OPTION = "model"

PLATFORM_GROUP = ("platform", "Mirasim")
OWN_KEY_GROUP = ("own-key", "Own key")

OWN_ROUTE = "own"
PLATFORM_ROUTE = "platform"
DEFAULT_ROUTE = "default"
"""The routes ``ui-cli session route --to`` takes; ``default`` leaves the session to Mirasim's own setting."""

PLATFORM_ROUTE_AGENTS = frozenset({"claude", "codex", "kimi", "zcode"})
"""The agents whose own models the Mirasim platform runs as well.

Named rather than read off the catalog, which does not say: ``routeByModel``
marks only the rows that take one route -- the platform's own models and the
user's keys -- and leaves every model of the agent's own unmarked, whether the
platform runs it or not.
"""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    """The value the menu offers, and a client sends back."""
    label: str
    route: str = ""
    """``relay``, ``direct``, or empty for the agent's own model."""
    rungs: tuple[str, ...] = ()
    model: str = ""
    """The id Mirasim runs, where it is not ``id``: the model behind a route prefix."""
    session_route: str = ""
    """The route a session is put on to run this value, ``own`` or ``platform``; empty leaves that to Mirasim."""


@dataclass(frozen=True)
class AgentCatalog:
    """One agent's facts and menus, as Mirasim reported them."""

    agent: str
    label: str
    installed: bool
    default_model: str | None = None
    default_effort: str | None = None
    models: tuple[ModelEntry, ...] = ()
    """The menu, in the order it is offered."""
    efforts: tuple[tuple[str, str], ...] = ()
    """The agent-level rungs as ``(id, label)``, which every model's list is drawn from."""
    menus_unread: bool = False
    """This catalog is empty because the read failed, not because the agent offers nothing.

    The two are one shape here -- ``models`` and ``efforts`` both empty -- and
    only one of them is a fact about the agent. A reader that cannot tell them
    apart records the failure as a measurement, and nothing invalidates a
    measurement, so a menu that was never read outlives every restart. Set by
    :func:`read_catalog` from the shape of its second document.
    """
    _by_id: dict[str, ModelEntry] = field(default_factory=dict, compare=False, repr=False)
    """Every value taken: the menu, and the bare ids of models it offers only behind a route."""

    def model(self, model_id: str | None) -> ModelEntry | None:
        return self._by_id.get(model_id or "")

    def mirasim_model(self, choice: str | None) -> str | None:
        """The id Mirasim runs for ``choice``: the model behind its route prefix, else ``choice`` itself."""
        entry = self.model(choice)
        return (entry.model or entry.id) if entry is not None else choice

    def route_for(self, choice: str | None) -> str | None:
        """The route a turn on ``choice`` runs on -- ``own``, ``platform``, or ``default`` for Mirasim's.

        ``None`` for an agent outside :data:`PLATFORM_ROUTE_AGENTS`: with no
        second route to choose, its sessions are never put on one, and stay on
        whatever Mirasim routes each model to. No choice at all is Mirasim's
        default, its route included.
        """
        if self.agent not in PLATFORM_ROUTE_AGENTS:
            return None
        entry = self.model(choice)
        return (entry.session_route if entry is not None else "") or DEFAULT_ROUTE

    def offers_model(self, model_id: str) -> bool:
        return model_id in self._by_id

    def offers_effort(self, effort: str) -> bool:
        return any(rung == effort for rung, _ in self.efforts)

    def rungs_for(self, model_id: str | None) -> tuple[str, ...]:
        """The effort rungs ``model_id`` takes -- the default model's when none is chosen.

        A model the catalog lists without rungs takes none. One missing from the
        catalog altogether gets the agent-level list: nothing narrower is known.
        """
        entry = self.model(model_id or self.default_model)
        if entry is not None:
            return entry.rungs
        return tuple(rung for rung, _ in self.efforts)

    def model_option(self, current: str | None) -> dict[str, Any] | None:
        """The ``model`` config option with ``current`` selected, or ``None`` when there is no menu."""
        if not self.models:
            return None
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in self.models:
            groups.setdefault(self._group_of(entry), []).append({"value": entry.id, "name": entry.label})
        value = current or self.default_model or self.models[0].id
        return {
            "id": MODEL_OPTION,
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": value,
            "options": [{"group": group, "name": name, "options": opts} for (group, name), opts in groups.items()],
        }

    def config_options(self, current: str | None) -> list[dict[str, Any]]:
        option = self.model_option(current)
        return [option] if option is not None else []

    def modes(self, current: str | None) -> dict[str, Any] | None:
        """The session's mode state, or ``None`` where no effort can be said to be current.

        That is an agent with no rungs, and one whose catalog names no default
        (ZCode CLI): a turn there runs on an effort Mirasim does not report, so
        any rung offered as current would be a guess -- and the menu itself is
        what Raven fits its session tier onto.
        """
        ids = [rung for rung, _ in self.efforts]
        if not ids or self.default_effort not in ids:
            return None
        return {
            "currentModeId": current if current in ids else self.default_effort,
            "availableModes": [
                {"id": rung, "name": name, "description": f"Reasoning effort: {name}"} for rung, name in self.efforts
            ],
        }

    def _group_of(self, entry: ModelEntry) -> tuple[str, str]:
        if entry.route == "relay" or entry.session_route == PLATFORM_ROUTE:
            return PLATFORM_GROUP
        if entry.route == "direct":
            return OWN_KEY_GROUP
        return ("agent", self.label)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _rungs(items: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(items, list):
        return ()
    out: list[tuple[str, str]] = []
    for item in items:
        rung = _text(item.get("id")) if isinstance(item, dict) else ""
        if rung:
            out.append((rung, _text(item.get("label")) or rung))
    return tuple(out)


def agent_entry(agents_doc: Any, agent: str) -> dict[str, Any] | None:
    """The ``agents[]`` row naming ``agent``, or ``None``."""
    rows = agents_doc.get("agents") if isinstance(agents_doc, dict) else None
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and row.get("id") == agent:
            return row
    return None


def read_catalog(agent: str, agents_doc: Any, catalog_doc: Any) -> AgentCatalog:
    """Build an :class:`AgentCatalog` from the two documents, tolerating a missing catalog.

    Anything not in the expected shape is dropped rather than guessed at: a
    guessed model would be offered to a reader and then refused by Mirasim.
    """
    row = agent_entry(agents_doc, agent) or {}
    label = _text(row.get("label")) or agent
    installed = bool(row.get("installed"))
    doc = catalog_doc if isinstance(catalog_doc, dict) else {}
    routes = doc.get("routeByModel") if isinstance(doc.get("routeByModel"), dict) else {}
    efforts_by_model = doc.get("effortByModel") if isinstance(doc.get("effortByModel"), dict) else {}
    listed: list[ModelEntry] = []
    seen: set[str] = set()
    for item in doc.get("models") if isinstance(doc.get("models"), list) else ():
        model_id = _text(item.get("id")) if isinstance(item, dict) else ""
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        route = routes.get(model_id)
        listed.append(
            ModelEntry(
                id=model_id,
                label=_text(item.get("label")) or model_id,
                route=route if route in ("relay", "direct") else "",
                rungs=tuple(rung for rung, _ in _rungs(efforts_by_model.get(model_id))),
            )
        )
    models, unlisted = _menu(agent, listed)
    return AgentCatalog(
        agent=agent,
        label=label,
        installed=installed,
        # Mirasim's own word even when its menu does not list it: this is what
        # a turn with no --model runs on, and a session restored to it has to
        # land there rather than on whatever the menu happens to list first.
        default_model=_text(doc.get("defaultModel")) or (listed[0].id if listed else None),
        default_effort=_text(doc.get("defaultEffort")) or None,
        models=tuple(models),
        efforts=_rungs(doc.get("effort")),
        # Not ``not models``: an agent that answered with a well-formed catalog
        # naming no model has offered nothing, and that IS a fact about it. Only
        # a document that never arrived is a read to take again.
        menus_unread=not isinstance(catalog_doc, dict),
        _by_id={entry.id: entry for entry in (*unlisted, *models)},
    )


def _menu(agent: str, listed: list[ModelEntry]) -> tuple[list[ModelEntry], list[ModelEntry]]:
    """``agent``'s menu, and the entries it takes without offering them.

    For an agent the platform also runs, each own model is offered once per
    route, under the agent's heading and then ahead of the platform's own
    models, the order Mirasim lists the two kinds in; its bare id is taken
    unlisted. Any other agent's menu is the catalog as it stands.
    """
    if agent not in PLATFORM_ROUTE_AGENTS:
        return listed, []
    own = [entry for entry in listed if not entry.route]
    taken = {entry.id for entry in listed}

    def on(route: str) -> list[ModelEntry]:
        return [
            replace(entry, id=f"{route}:{entry.id}", model=entry.id, session_route=route)
            for entry in own
            if f"{route}:{entry.id}" not in taken
        ]

    return [*on(OWN_ROUTE), *on(PLATFORM_ROUTE), *(entry for entry in listed if entry.route)], own


__all__ = [
    "DEFAULT_ROUTE",
    "MODEL_OPTION",
    "OWN_KEY_GROUP",
    "OWN_ROUTE",
    "PLATFORM_GROUP",
    "PLATFORM_ROUTE",
    "PLATFORM_ROUTE_AGENTS",
    "AgentCatalog",
    "ModelEntry",
    "agent_entry",
    "read_catalog",
]
