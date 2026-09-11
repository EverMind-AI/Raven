"""``playbooks.*`` RPC handlers -- the page's view of the playbook library.

Read-only, and deliberately two calls rather than one:

* ``playbooks.list`` answers a row per playbook *including the graph's shape*
  (each node's id and what it depends on, nothing else). The library page draws
  one concept diagram per card, and a card that had to fetch its own graph
  would turn opening the page into N round trips.
* ``playbooks.get`` answers one whole spec -- per-node prompts, skills, mcps,
  instance handles -- which is what the detail view's node panel reads.

A file that will not parse is a **row**, not a failed call: the library is two
directories of user-editable text, and one bad file must not take the page down
with it. Such a row carries ``error`` and an empty ``nodes``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.playbook import PlaybookSpec, PlaybookStore
    from raven.rpc.dispatcher import Dispatcher


def _store() -> PlaybookStore:
    """The two-layer library, resolved the same way the CLI resolves it.

    Built per call rather than cached: the user layer is a directory a person
    edits between calls, and the store holds a path, not an index.
    """
    from pathlib import Path

    from raven.config.loader import load_config
    from raven.playbook import PlaybookStore

    config = load_config()
    user_layer = Path(config.playbooks.dir) if config.playbooks.dir else (config.workspace_path / "playbooks")
    return PlaybookStore(user_layer)


def _disabled() -> set[str]:
    from raven.config.loader import load_config

    return set(load_config().playbooks.disabled)


def _shape(spec: PlaybookSpec) -> list[dict[str, Any]]:
    """Just enough of the graph to draw it: who is a step, and what waits on what.

    The card's diagram needs no prompt and no agent name, and a list that
    carried them would ship every template in the library on page open.
    """
    return [{"id": node.id, "depends_on": list(node.depends_on)} for node in (spec.nodes or [])]


def _row(store: PlaybookStore, name: str, disabled: set[str]) -> dict[str, Any]:
    origin = store.origin_of(name) or "user"
    row: dict[str, Any] = {
        "name": name,
        "origin": "user (shadows builtin)" if store.is_shadowing(name) else origin,
        "disabled": name in disabled,
        "description": "",
        "task_summary": "",
        "mode": "dag",
        "confirm": True,
        "nodes": [],
        "error": "",
    }
    try:
        spec = store.load(name)
    except Exception as exc:  # noqa: BLE001 - a broken file is a row, not a crash
        row["error"] = str(exc)
        return row
    row["description"] = spec.description
    row["task_summary"] = spec.task_summary
    row["mode"] = spec.mode
    row["confirm"] = spec.confirm
    row["nodes"] = _shape(spec)
    return row


def _node_wire(node: Any) -> dict[str, Any]:
    """One node as the detail view reads it.

    Explicit rather than ``model_dump``: the page's contract must not change
    shape because the graph model grew a field. Two spellings of "nothing here",
    deliberately different -- a blank ``subagent`` / ``node_summary`` /
    ``prompt_template`` is an empty string (the author left it for the caller to
    fill, and the field still exists), while an unwritten ``skills`` / ``mcps``
    is an absent key (see below).
    """
    wire: dict[str, Any] = {
        "id": node.id,
        "subagent": node.subagent or "",
        "node_summary": node.node_summary or "",
        "prompt_template": node.prompt_template or "",
        "depends_on": list(node.depends_on),
        "instance": node.instance or "",
        "inputs": dict(node.inputs) if getattr(node, "inputs", None) else {},
    }
    # Three-state, and the third state is the key being absent: "the author wrote
    # nothing" is what the contract spells by leaving these two out, while `[]`
    # is a real instruction the author gave. Sending an explicit null instead
    # would make the wire disagree with the schema the client is typed from.
    if node.skills is not None:
        wire["skills"] = list(node.skills)
    if node.mcps is not None:
        wire["mcps"] = list(node.mcps)
    return wire


async def playbooks_list(params: dict) -> dict:
    """Every playbook in both layers, with the shape of each graph."""
    store = _store()
    disabled = _disabled()
    return {"playbooks": [_row(store, name, disabled) for name in store.list_ids()]}


async def playbooks_get(params: dict) -> dict:
    """One playbook, whole: params, nodes, and where the file lives."""
    from raven.rpc.errors import ConfigValidationError

    name = str(params.get("name") or "").strip()
    if not name:
        raise ConfigValidationError("name is required")
    store = _store()
    if store.origin_of(name) is None:
        raise ConfigValidationError(f"no playbook named {name}")
    spec = store.load(name)
    # Imported here rather than at module scope: this module is loaded to
    # register RPC methods, and the MCP client package pulls the SDK in with it.
    from raven.mcp.client import resolve_transport
    from raven.mcp.oauth import declares_own_endpoints

    return {
        "playbook": {
            "name": spec.name,
            "description": spec.description,
            "task_summary": spec.task_summary,
            "version": spec.version,
            "mode": spec.mode,
            "confirm": spec.confirm,
            "origin": "user (shadows builtin)" if store.is_shadowing(name) else (store.origin_of(name) or "user"),
            "disabled": name in _disabled(),
            "path": str(store.path_for(name)),
            "keywords": list(spec.triggers.keywords),
            "params": {
                key: {
                    "type": p.type,
                    "required": p.required,
                    "default": p.default,
                    "enum": list(p.enum) if p.enum else None,
                    "description": p.description,
                }
                for key, p in spec.params.items()
            },
            "nodes": [_node_wire(n) for n in (spec.nodes or [])],
            "prompts": spec.prompts or "",
            # The declarations, not resolved values: a carried server references
            # a credential through `{{ params.X }}` and the run supplies it, so
            # what the file holds is the reference and that is what goes out. A
            # node's `mcps` entry is only a name, and without this a reader
            # cannot tell a server the playbook ships from a host server that
            # happens to share the name.
            "mcp_servers": {
                name: {
                    # Every field the runtime reads to decide what this is and
                    # whether it runs -- `resolve_transport` consumes `type`,
                    # grant resolution consumes `enabled` and `auth`, and a tool
                    # call consumes the timeout. A projection missing any of them
                    # shows a disabled SSE server with OAuth as a launchable
                    # generic http one.
                    # The transport the runtime will pick, not the raw field: a
                    # url ending `/sse` resolves to `sse` and reporting the
                    # unwritten field as null both breaks the contract (the schema
                    # allows the three strings or an absent key) and leaves the
                    # reader to redo a guess this already knows the answer to.
                    **({"type": transport} if (transport := resolve_transport(cfg)) else {}),
                    "command": cfg.command or "",
                    "args": list(cfg.args or []),
                    "url": cfg.url or "",
                    "env": dict(cfg.env or {}),
                    "headers": dict(cfg.headers or {}),
                    "tool_timeout": cfg.tool_timeout,
                    "enabled": cfg.enabled,
                    "auth": cfg.auth,
                    # Whether one is declared, never what it is: the endpoints and
                    # any client id are the deployment's business. Asked through
                    # the predicate the OAuth path itself uses -- a partial
                    # document is ignored there and discovery runs, so calling it
                    # self-carried here would describe a server that does not
                    # exist.
                    "has_oauth_config": declares_own_endpoints(cfg),
                }
                for name, cfg in (spec.mcp_servers or {}).items()
            },
        }
    }


def register_playbooks_methods(dispatcher: Dispatcher) -> None:
    dispatcher.register("playbooks.list", playbooks_list)
    dispatcher.register("playbooks.get", playbooks_get)


__all__ = [
    "playbooks_get",
    "playbooks_list",
    "register_playbooks_methods",
]
