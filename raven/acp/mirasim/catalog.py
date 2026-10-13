"""What one mirasim agent offers, read from ``ui-cli agents`` and ``ui-cli catalog``.

Read once when the adapter starts and again when the copy is old, never per
turn: each read starts a mirasim server of its own, measured at five to seven
seconds. The menus are advertised in the shapes Raven already reads off any ACP
agent -- the model as the ``session/new`` config option whose ``category`` is
``model``, and the effort rungs as session modes -- so the model picker, a
per-instance model and the session tier reach a mirasim agent unchanged.

Three kinds of model share one catalog, told apart by ``routeByModel``: a row
routed ``relay`` runs on the mirasim platform's quota, one routed ``direct`` on
a key the user added to mirasim (its id is ``profile:<uuid>``), and an unrouted
row is the agent's own, served by its own sign-in. The menu groups them under
those three headings, because the same model can sit in two of them -- a
platform ``deepseek-flash`` and an own-key profile that also reaches DeepSeek --
and the label alone cannot say which one bills where.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODEL_OPTION = "model"

PLATFORM_GROUP = ("platform", "mirasim platform")
OWN_KEY_GROUP = ("own-key", "Own key")


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str
    route: str = ""
    """``relay``, ``direct``, or empty for the agent's own model."""
    rungs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCatalog:
    """One agent's facts and menus, as mirasim reported them."""

    agent: str
    label: str
    installed: bool
    default_model: str | None = None
    default_effort: str | None = None
    models: tuple[ModelEntry, ...] = ()
    efforts: tuple[tuple[str, str], ...] = ()
    """The agent-level rungs as ``(id, label)``, which every model's list is drawn from."""
    _by_id: dict[str, ModelEntry] = field(default_factory=dict, compare=False, repr=False)

    def model(self, model_id: str | None) -> ModelEntry | None:
        return self._by_id.get(model_id or "")

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
        (ZCode CLI): a turn there runs on an effort mirasim does not report, so
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
        if entry.route == "relay":
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
    guessed model would be offered to a reader and then refused by mirasim.
    """
    row = agent_entry(agents_doc, agent) or {}
    label = _text(row.get("label")) or agent
    installed = bool(row.get("installed"))
    doc = catalog_doc if isinstance(catalog_doc, dict) else {}
    routes = doc.get("routeByModel") if isinstance(doc.get("routeByModel"), dict) else {}
    efforts_by_model = doc.get("effortByModel") if isinstance(doc.get("effortByModel"), dict) else {}
    models: list[ModelEntry] = []
    seen: set[str] = set()
    for item in doc.get("models") if isinstance(doc.get("models"), list) else ():
        model_id = _text(item.get("id")) if isinstance(item, dict) else ""
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        route = routes.get(model_id)
        models.append(
            ModelEntry(
                id=model_id,
                label=_text(item.get("label")) or model_id,
                route=route if route in ("relay", "direct") else "",
                rungs=tuple(rung for rung, _ in _rungs(efforts_by_model.get(model_id))),
            )
        )
    return AgentCatalog(
        agent=agent,
        label=label,
        installed=installed,
        # mirasim's own word even when its menu does not list it: this is what
        # a turn with no --model runs on, and a session restored to it has to
        # land there rather than on whatever the menu happens to list first.
        default_model=_text(doc.get("defaultModel")) or (models[0].id if models else None),
        default_effort=_text(doc.get("defaultEffort")) or None,
        models=tuple(models),
        efforts=_rungs(doc.get("effort")),
        _by_id={entry.id: entry for entry in models},
    )


__all__ = [
    "MODEL_OPTION",
    "OWN_KEY_GROUP",
    "PLATFORM_GROUP",
    "AgentCatalog",
    "ModelEntry",
    "agent_entry",
    "read_catalog",
]
