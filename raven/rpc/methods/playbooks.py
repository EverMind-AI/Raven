"""``playbooks.*`` RPC handlers -- the playbook library as a client sees it.

The reads are deliberately two calls rather than one:

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

import re
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.playbook import PlaybookSpec, PlaybookStore
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.session import AgentLoopFactory


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

    A ``rounds`` playbook answers with its roles. It has no ``nodes`` -- a round
    is compiled into them when it is dispatched -- but the picture the reader
    wants is the same picture: who runs, and in what order. Answering with an
    empty list drew a card with nothing on it, which reads as a playbook that
    does nothing.
    """
    if spec.mode == "stint":
        return [{"id": role.label, "depends_on": list(role.depends_on)} for role in (spec.roles or [])]
    return [{"id": node.id, "depends_on": list(node.depends_on)} for node in (spec.nodes or [])]


def _stint_wire(spec: PlaybookSpec) -> dict[str, Any]:
    """The four sections `mode: stint` adds, as the page reads them.

    Explicit rather than ``model_dump`` for the reason ``_node_wire`` is: the
    page's contract must not change shape because a spec model grew a field.

    ``run`` goes out in full. It is a shell command the stint will execute on the
    reader's machine, and the one moment they are asked to approve that is the
    moment they are looking at this.
    """
    from raven.playbook.stint import terminal_roles
    from raven.playbook.stint_spec import DEFAULT_MAX_ROUNDS

    last = terminal_roles(spec)
    stop = spec.stop
    return {
        "roles": [
            {
                "label": role.label,
                "agent": role.name,
                "node_summary": role.node_summary,
                "depends_on": list(role.depends_on),
                "owns": list(role.owns),
                "appends": list(role.appends),
                "reads": list(role.reads),
                "enforce_read": role.enforce.read,
                "enforce_write": role.enforce.write,
                "journal_section": role.journal_section,
                "verify_after": list(role.verify_after),
                "max_handbacks": role.max_handbacks,
                "terminal": role.label in last,
            }
            for role in (spec.roles or [])
        ],
        "carried": [
            {
                "path": entry.path,
                "append": entry.append,
                "recent_rounds": entry.recent_rounds,
                "max_chars": entry.max_chars,
            }
            for entry in (spec.memory or [])
        ],
        "checks": [
            {
                "name": check.name,
                # A check may be declared by description instead, and then the
                # command is the project's rather than the playbook's: the page
                # is showing the file, so it shows what the file says.
                "run": check.run or f"({check.description})",
                "timeout_sec": check.timeout_sec,
                "needs_display": check.needs_display,
            }
            for check in (spec.verify or [])
        ],
        "max_rounds": stop.max_rounds if stop is not None else DEFAULT_MAX_ROUNDS,
        "until": stop.until if stop is not None else "",
        "report": stop.report if stop is not None else "round",
    }


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
    name = _known_name(params.get("name"))
    store = _store()
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
            # Only where there is one. A page that reads this key on a dag
            # playbook is asking about a section the file does not have, and an
            # empty one would answer "no roles, no checks, stops after 0 rounds"
            # -- three statements about a stint that does not exist.
            **({"stint": _stint_wire(spec)} if spec.mode == "stint" else {}),
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


# ── playbooks.credentials.* / playbooks.oauth.* ─────────────────────────────
#
# The machine-held half of a carried server's credential (raven/playbook/
# credentials.py). Nothing here returns a secret's value: ``get`` says which
# params are set and which OAuth servers hold tokens, and that is all a page
# needs to draw the tab.


def _kebab_name(raw: Any) -> str:
    """One playbook's name, checked for shape and nothing else.

    The shape check is not cosmetic and is not the store's job. A name is joined
    to the library root to resolve a directory, so ``../sibling`` resolves
    *outside* the library and is classified as a user playbook -- which made
    every name-taking handler here read, and one of them delete, a path the
    caller chose. ``NAME_RE`` is the boundary the library already defines and
    ``raven playbook create`` already enforces; this is the same rule on the way
    in rather than only on the way out.

    Separate from the existence check because the two callers want opposite
    answers to it: every handler that acts on a playbook needs the name to
    resolve, and creation needs it not to. Sharing the shape rule rather than
    the whole guard is what keeps those two from drifting apart.

    Answers the clean name so a caller cannot go on using the raw one.
    """
    import re

    from raven.playbook.types import NAME_RE
    from raven.rpc.errors import ConfigValidationError

    name = str(raw or "").strip()
    if not name:
        raise ConfigValidationError("name is required")
    if not re.fullmatch(NAME_RE, name):
        raise ConfigValidationError(f"playbook names are kebab-case ({NAME_RE}); got {name!r}")
    return name


def _known_name(raw: Any) -> str:
    """One playbook's name, checked for shape and then for existence.

    The shape goes first, because the lookup is itself the escape: ``origin_of``
    answers ``user`` for a traversal, so a guard placed after it has already
    been told the wrong answer.
    """
    from raven.rpc.errors import ConfigValidationError

    name = _kebab_name(raw)
    if _store().origin_of(name) is None:
        raise ConfigValidationError(f"no playbook named {name}")
    return name


def _spec_or_raise(name: str):
    return _store().load(_known_name(name))


async def playbooks_credentials_get(params: dict) -> dict:
    """Which of a playbook's secret params and OAuth servers this machine holds -- never the values."""
    from raven.playbook.credentials import has_oauth_tokens, stored_secret_param_names
    from raven.playbook.params import secret_param_names

    spec = _spec_or_raise(params.get("name"))
    stored = stored_secret_param_names(spec.name)
    host_names = set(_host_mcp_server_names())
    return {
        "params": [
            {"name": pname, "set": pname in stored, "description": spec.params[pname].description}
            for pname in sorted(secret_param_names(spec))
        ],
        "servers": [
            {
                "name": sname,
                "auth": cfg.auth,
                "enabled": cfg.enabled,
                "authorized": bool(cfg.auth == "oauth" and has_oauth_tokens(sname, spec.name)),
                "shadows_host": sname in host_names,
            }
            for sname, cfg in sorted((spec.mcp_servers or {}).items())
        ],
    }


def _host_mcp_server_names() -> list[str]:
    try:
        from raven.config.loader import load_config

        return list((load_config().tools.mcp_servers or {}).keys())
    except Exception:  # noqa: BLE001 - a page without a readable host config still gets the tab
        return []


async def playbooks_credentials_set(params: dict) -> dict:
    """Store one secret param's value for a playbook. Refused for a param the spec does not declare secret."""
    from raven.playbook.credentials import set_secret_param
    from raven.rpc.errors import ConfigValidationError

    spec = _spec_or_raise(params.get("name"))
    pname = str(params.get("param") or "").strip()
    declared = spec.params.get(pname)
    if declared is None or declared.type != "secret":
        raise ConfigValidationError(f"{spec.name} declares no secret param named {pname or '<empty>'}")
    value = params.get("value")
    if not isinstance(value, str) or not value:
        raise ConfigValidationError("value is required")
    set_secret_param(spec.name, pname, value)
    return {"ok": True}


async def playbooks_credentials_clear(params: dict) -> dict:
    from raven.playbook.credentials import clear_secret_param

    spec = _spec_or_raise(params.get("name"))
    pname = str(params.get("param") or "").strip()
    if pname:
        clear_secret_param(spec.name, pname)
    return {"ok": True}


def _carried_oauth_server(spec, server: str):
    from raven.rpc.errors import ConfigValidationError

    cfg = (spec.mcp_servers or {}).get(server)
    if cfg is None:
        raise ConfigValidationError(f"{spec.name} carries no MCP server named {server or '<empty>'}")
    if cfg.auth != "oauth":
        raise ConfigValidationError(f"{server} has auth={cfg.auth!r}; only an oauth server can be authorized")
    return cfg


async def playbooks_oauth_authorize(params: dict) -> dict:
    """Start the browser OAuth flow for a carried server, under the playbook's own credential scope.

    A throwaway manager rather than the host's: the host's is keyed by bare
    server name, and a carried server may shadow a host server of that name.
    The call answers within ``_AUTHORIZE_WAIT_S`` with what the connect reached
    -- usually the URL it parked on, which the page shows; the flow itself keeps
    running behind the answer and the tokens land in the scoped file.
    """
    from raven.agent.tools.registry import ToolRegistry
    from raven.market.connect import PlugConnectError, await_authorization
    from raven.mcp.manager import MCPConnectionManager
    from raven.mcp.oauth import pending_url
    from raven.playbook.credentials import credential_scope

    spec = _spec_or_raise(params.get("name"))
    server = str(params.get("server") or "").strip()
    cfg = _carried_oauth_server(spec, server)
    manager = MCPConnectionManager(ToolRegistry(), credential_scope=credential_scope(spec.name))

    async def _connect_then_close() -> dict:
        try:
            return await manager.connect(server, cfg, interactive=True)
        finally:
            await manager.aclose()

    # The same wait ``plug.auth`` gets, and the failure reported the way this
    # method already reports a degraded state: in ``error``. Raising instead
    # loses the diagnostic at the wire, where a frame carries the code name in
    # ``message`` and the reason in ``data`` -- and the page toasts
    # ``message``, so the reader would see ``config_validation_error`` where the
    # truth is "the sandbox could not start".
    try:
        snap = await await_authorization(manager, server, _connect_then_close)
    except PlugConnectError as e:
        logger.warning("playbooks.oauth.authorize: {!r} failed: {}", server, e.detail)
        return {"server": server, "state": "error", "auth_url": None, "error": e.detail}
    except Exception as e:  # noqa: BLE001 - the reason is the whole point of catching it
        logger.warning("playbooks.oauth.authorize: {!r} failed: {}", server, e)
        return {"server": server, "state": "error", "auth_url": None, "error": str(e)}
    return {
        "server": server,
        "state": (snap or {}).get("state") or "connecting",
        "auth_url": pending_url(server),
        "error": (snap or {}).get("error"),
    }


async def playbooks_oauth_clear(params: dict) -> dict:
    from raven.playbook.credentials import clear_oauth_tokens

    spec = _spec_or_raise(params.get("name"))
    server = str(params.get("server") or "").strip()
    _carried_oauth_server(spec, server)
    clear_oauth_tokens(server, spec.name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# The library as a thing a person changes and runs, not only reads.
#
# Everything below is reachable from `raven playbook`, and the rule these
# handlers keep is that they reach it through the same door rather than around
# it: enabling writes the deny list the CLI writes, running goes through the
# runtime the model's own tool goes through, and creation binds the composer
# both creation entries bind. A second path to the same library would be a
# second place for its rules to live.


async def playbooks_set_enabled(params: dict) -> dict:
    """Take a playbook off the deny list, or put it on it.

    ``enabled`` is the state a caller wants, not a toggle: a toggle makes two
    clients racing on one name land wherever the ordering falls, and the page
    already knows which state it is asking for.

    Disabling takes a playbook out of what the model is offered and nothing
    else. It stays runnable by name from the CLI, which is the user's own hand
    rather than the model's -- the same rule ``raven playbook disable`` states.

    Answers with the state now in force and whether this call is what changed
    it, so a caller can tell "you did that" from "it was already so" without a
    second read.
    """
    from raven.config.update import set_playbook_disabled
    from raven.rpc.errors import ConfigValidationError

    name = _known_name(params.get("name"))
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigValidationError("enabled must be true or false")
    changed = set_playbook_disabled(name, not enabled)
    return {"name": name, "enabled": enabled, "changed": bool(changed)}


def _known_agent_names(agent_loop_factory: "AgentLoopFactory | None") -> list[str]:
    """The agent table a dispatch would resolve against, as it stands now.

    The live registry first, and the config file only when there is no loop to
    ask. The two are not always the same table: the agents list is deliberately
    kept off a watcher and moved only by an explicit apply, so a row written by
    another process -- ``raven agents new --register`` is the documented one --
    is on disk and not yet in this process. Read from the file here, a step
    naming that agent would validate clean and then be refused at dispatch,
    which is the reverse of what a check is for: this answers "would it run",
    and the only table that can say is the one the run would use.
    """
    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:  # noqa: BLE001 - no loop is a fallback, not a failure
            loop = None
    registry = getattr(getattr(loop, "subagents", None), "registry", None)
    if registry is not None:
        return list(registry.all_names())

    from raven.agent.subagent.registry import AgentRegistry
    from raven.config.loader import load_config

    offline = AgentRegistry()
    offline.apply(load_config().subagents.agents)
    return list(offline.all_names())


async def playbooks_validate(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """Check one playbook without running it: the spec's shape, then its rules.

    Answers rather than raises. A playbook that does not validate is the normal
    reason to call this, so the findings are the result -- an error code would
    make the ordinary answer look like a broken call, and carries one string
    where this carries the list.

    Validated against this machine's agent table, because a playbook is a
    distribution unit: a step naming an agent this host does not have is a real
    finding here, and the CLI reports it the same way.
    """
    import yaml
    from pydantic import ValidationError

    from raven.playbook.validate import validate_structure

    name = _known_name(params.get("name"))
    store = _store()

    errors: list[str] = []
    spec = None
    try:
        spec = store.load(name)
    # ``yaml.YAMLError`` beside the other two: a file with an unclosed bracket
    # raises ``ParserError``, which descends from neither, and a hand-edited
    # playbook is the commonest way this breaks. Left out, the one method whose
    # whole point is to answer with findings turned that case into an internal
    # error with a traceback -- the shape its own contract exists to avoid.
    except (ValidationError, ValueError, yaml.YAMLError) as exc:
        errors.append(str(exc))
    if spec is not None:
        errors.extend(validate_structure(spec, known_agents=_known_agent_names(agent_loop_factory)))
    return {"name": name, "ok": not errors, "errors": errors, "path": str(store.path_for(name))}


async def playbooks_delete(params: dict) -> dict:
    """Remove a user playbook's directory.

    Refused for a builtin, which ships with the package: there is no file of the
    host's to remove, and the next install would put it back. Disabling is the
    operation that exists for those, and the refusal says so.

    The name comes off the deny list as it goes, for the reason the CLI records:
    a gone name has no business there, and a later playbook reusing it should
    start enabled like any other new one.

    A user playbook shadowing a builtin of the same name is the case worth
    knowing about: deleting it does not remove the name, it uncovers the
    builtin. The answer says which happened rather than leaving a caller to
    re-read the list to find out.
    """
    import shutil

    from raven.config.update import set_playbook_disabled
    from raven.rpc.errors import ConfigValidationError

    name = _known_name(params.get("name"))
    store = _store()
    if store.origin_of(name) == "builtin":
        raise ConfigValidationError(f"{name} is a builtin and cannot be deleted; disable it instead")

    # One resolution, and it is the one that gets removed. Asking which layer
    # serves the name and then asking which directory to remove are two reads of
    # a disk a second process also writes -- `raven playbook delete` is that
    # process -- and the second read is the one `rmtree` acts on. Answering both
    # from a single lookup means the path removed is one that was in the user
    # layer when it was resolved, whatever happened in between.
    directory = store.user_directory(name)
    if directory is None:
        raise ConfigValidationError(f"{name} is no longer in the library; nothing was removed")
    shutil.rmtree(directory)
    set_playbook_disabled(name, False)
    # Read back through a fresh store: `_store()` holds paths rather than an
    # index, so this is what the library actually offers now.
    uncovered = _store().origin_of(name) == "builtin"
    return {"name": name, "deleted": True, "uncovered_builtin": uncovered}


#: Whether a call arriving on this socket counts as the user naming the playbook
#: themselves. The two entries that existed answer differently, and both say why:
#: ``PlaybookRuntime.load``'s ``allow_disabled`` is "for the CLI, where the user
#: named the playbook themselves", while the conversation path leaves it false
#: because there the model chose. A client here holds the session cookie, which is
#: the user's own credential and not something a model is handed, so it is read as
#: the user's hand -- disabling takes a playbook out of what the *model* is
#: offered, and this caller is not the model.
_CALLER_NAMED_IT = True


async def playbooks_run(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """Run one playbook, through the same entry the model's tool and the CLI use.

    Answers the executor's own plan verbatim -- ``kind`` and ``reply`` -- rather
    than a shape of this layer's devising. A ``dag`` playbook dispatches and the
    reply is the receipt (the run id is in it, and ``_run_id_of`` states why it
    lives in the text rather than in a field of its own); a ``prompt`` one comes
    back as composition guidance; ``gaps`` means nothing was dispatched and names
    what is still needed; ``questions`` says why it cannot proceed.

    ``session_key`` is required, and is the whole reason this is not a thinner
    wrapper. A run's progress and its completion announce are addressed to a
    conversation, which the loop sets per turn on every origin it knows about.
    An RPC call is an origin it does not: the address is a ``ContextVar`` whose
    every consumer falls back to a ``cli:direct`` default, so a page-initiated
    run would report somewhere nobody is looking. The key is the conversation's
    own ``channel:chat_id``, the same pair every origin dict carries.

    ``confirmed`` is the caller's own statement that it already put this run to
    the user, which is what the executor's gate reads to avoid asking a second
    time. Taken from the caller rather than assumed here: a page that shows a
    confirmation and one that fires on a single click are both legitimate, and
    only the page knows which it is. Asserting it on their behalf would skip the
    gate with nobody having seen the graph.
    """
    from raven.agent import workdir
    from raven.providers.binding import use_binding
    from raven.rpc.errors import ConfigValidationError

    name = _known_name(params.get("name"))

    session_key = str(params.get("session_key") or "").strip()
    channel, sep, chat_id = session_key.partition(":")
    if not (sep and channel and chat_id):
        raise ConfigValidationError(f"session_key must be a conversation's channel:chat_id; got {session_key!r}")

    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:  # noqa: BLE001 - no loop is a refusal, not a crash
            loop = None
    runtime = getattr(loop, "_playbooks", None)
    if runtime is None:
        # A write, so it refuses rather than degrading to empty: answering a run
        # with "nothing happened" would leave the caller drawing a dispatch that
        # was never made. Both causes read the same to a caller and are named
        # together -- playbooks switched off builds no runtime at all.
        raise ConfigValidationError("playbooks are not running on this host, so nothing can be dispatched")

    # Addressed before the dispatch, not after: the origin is read inside
    # ``execute``, so setting it afterwards would arrive for the next call.
    runtime.set_context(channel=channel, chat_id=chat_id, session_key=session_key)

    try:
        session_workdir = loop.session_workdir(session_key)
    except Exception as exc:  # noqa: BLE001 - a bad override is the caller's to fix
        raise ConfigValidationError(
            f"session {session_key!r} has no usable working directory; clear its override to recover"
        ) from exc

    # Two contexts, entered around the dispatch rather than left to their
    # defaults. The turn path establishes both and a graph reads both: the tool
    # takes its nodes' cwd from ``workdir.current() or self._workspace``, and
    # resolves a raven-backed node's pair through the active binding. Outside
    # them a run aimed at one session would work in the loop-wide workspace and
    # on the default model while reporting to that session -- the wrong project
    # and the wrong model, under the right conversation's name.
    #
    # Around ``load``, not merely before it: the dispatch backgrounds itself with
    # ``create_task``, which copies the context it is created in, and a binding
    # that has already been reset by then is one the detached run never sees.
    with workdir.bind(session_workdir), use_binding(loop.binding_for_session(session_key)):
        stint = await runtime.load(
            name,
            params.get("params") or {},
            params.get("fills") or {},
            allow_disabled=_CALLER_NAMED_IT,
            confirmed=bool(params.get("confirmed")),
        )
    if stint is None:
        # ``_known_name`` already proved the directory exists, so a miss here is
        # the runtime's own view disagreeing: a file that will not parse is absent
        # from it. Told apart rather than reported as one, because one is fixed by
        # editing the file and the other by enabling the playbook -- and with
        # ``allow_disabled`` true above, only the first can actually reach here.
        raise ConfigValidationError(f"playbook {name!r} does not load; validate it to see why")
    return {"name": name, "kind": stint.kind, "reply": stint.reply}


def _generation_budget_s() -> float:
    """How long one generation may take, read from the entry that declares it.

    Taken from the conversational tool's own ``timeout_seconds`` rather than
    written again here. Creation is a draft plus repair rounds and the two
    entries should not disagree about how long that may take -- and a number
    copied is a number that drifts, where a number read moves for both the
    moment either is reconsidered.
    """
    from raven.agent.tools.create_playbook import CreatePlaybookTool

    return float(CreatePlaybookTool.timeout_seconds)


async def playbooks_create(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """Generate a playbook from a description of the work and store it.

    Takes a description, never a spec. Generation, validation and repair stay
    inside ``PlaybookGenerator``, which is what keeps a caller from writing an
    arbitrary graph into the library through this door -- the same rule the
    conversational entry states for itself, and the reason neither offers a
    field for the nodes.

    The two pre-flight checks refuse: a name that is not kebab-case, and a name
    the library already holds. Both are single facts, and both are worth
    catching before spending a generation rather than after.

    A generation that fails does not refuse. It produces a *list* of reasons the
    composer could not resolve, and an error code carries one string; a caller
    that has just spent a minute of model time on this is owed the list rather
    than a flattened sentence. ``created`` tells the two halves apart, and
    ``errors`` is non-empty exactly when it is false.

    ``notes`` are the generator's own open questions -- assumptions it made and
    gaps it could not close. They are written into the file's prose for a human
    to review and are returned here so a client need not read the file back to
    show them.
    """
    import asyncio

    from raven.playbook import PlaybookExistsError, PlaybookGenerationError
    from raven.rpc.errors import ConfigValidationError

    name = _kebab_name(params.get("name"))
    workflow = str(params.get("workflow") or "").strip()
    if not workflow:
        raise ConfigValidationError("workflow is required: the generator sees only this text")
    raw_skills = params.get("skills") or []
    skills = [str(s) for s in raw_skills if str(s).strip()] if isinstance(raw_skills, list) else []

    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:  # noqa: BLE001 - no loop is a refusal, not a crash
            loop = None
    runtime = getattr(loop, "_playbooks", None)
    if runtime is None:
        raise ConfigValidationError("playbooks are not running on this host, so nothing can be generated")

    store = runtime.store
    if store.origin_of(name) is not None:
        raise ConfigValidationError(
            f"a {store.origin_of(name)} playbook named {name!r} already exists; pick another name"
        )

    budget = _generation_budget_s()
    try:
        generated = await asyncio.wait_for(runtime.generator.generate(workflow, skills), budget)
    except PlaybookGenerationError as exc:
        return {
            "name": name,
            "created": False,
            "path": "",
            "notes": [],
            "errors": list(getattr(exc, "errors", None) or [str(exc)]),
            "adopted": False,
        }
    except TimeoutError:
        return {
            "name": name,
            "created": False,
            "path": "",
            "notes": [],
            "errors": [f"generation did not finish within {budget:.0f}s"],
            "adopted": False,
        }

    spec = generated.spec.model_copy(update={"name": name})
    try:
        path = store.save(spec, notes=generated.notes)
    except PlaybookExistsError as exc:
        # The name was free at the pre-flight and taken by the time the
        # generation finished. The file on disk is the other request's and is
        # kept, so this is a refusal rather than a result claiming a write.
        raise ConfigValidationError(
            f"playbook {name!r} was created while this one was generating; the existing file was kept"
        ) from exc

    # Handed to the live library in the same call, as the conversational entry
    # does. Reported rather than assumed: a playbook written but not loadable is
    # a different state from one that is ready, and "created" alone would send a
    # caller to run a name that cannot resolve.
    adopted = bool(runtime.adopt(name))
    return {
        "name": name,
        "created": True,
        "path": str(path),
        "notes": list(generated.notes or []),
        "errors": [],
        "adopted": adopted,
    }


def _stint_stores() -> list[Any]:
    """Every stint store on this machine, one per conversation.

    A stint is kept beside the conversation that started it, and the page lists
    the machine's stints rather than one conversation's. Reading a single store
    showed an empty page while a stint was running, which reads as "nothing has
    run" and not as "you are looking in the wrong place".

    Still only files: a stint is read far more often than it is run -- what round
    is it on, what did it undo, what is it waiting for -- and constructing the
    sub-agent stack to answer would make opening the page cost what a dispatch
    costs.
    """
    from raven.agent.subagent.history import dag_root
    from raven.config.loader import load_config
    from raven.session.manager import SessionManager
    from raven.stint.record import STINTS_DIRNAME, StintStore

    sessions = SessionManager(load_config().workspace_path).sessions_dir
    roots = [dag_root(path) / STINTS_DIRNAME for path in sorted(sessions.glob("*/*")) if path.is_dir()]
    return [StintStore(root) for root in roots if root.is_dir()]


#: The shape `make_stint_id` mints, and what a store file is named after.
STINT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _stint_row(record: Any) -> dict[str, Any]:
    from raven.playbook.stint_spec import DEFAULT_MAX_ROUNDS

    stop = record.spec.get("stop") if isinstance(record.spec, dict) else None
    if not isinstance(stop, dict):
        stop = {}
    return {
        "stint_id": record.stint_id,
        "playbook": record.playbook,
        "round_index": record.round_index,
        # The budget the driver runs to, which a spec with no `stop:` section
        # still has: reporting nought there drew "round 3 of 0" on the page.
        "max_rounds": int(stop.get("maxRounds") or stop.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "status": record.status,
        "live": record.live,
        # What `stop` acts on, which is wider than `live`: a paused stint is not
        # live and still has to be stoppable, or the page draws no way to end it.
        "unfinished": record.unfinished,
        "stop_reason": record.stop_reason,
        "workdir": record.workdir,
        "branch": record.branch,
        "started_at_ms": record.started_at_ms,
        "ended_at_ms": record.ended_at_ms,
        "open_questions": sum(1 for q in record.questions if not str(q.get("answer") or "").strip()),
    }


def _stint_detail(record: Any) -> dict[str, Any]:
    return {
        "stint": _stint_row(record),
        "rounds": [
            {
                "index": entry.index,
                "run_id": entry.run_id,
                "attempt": entry.attempt,
                "status": entry.status,
                "checks": [f"{row.get('name')}={row.get('status')}" for row in entry.verify],
                "violations": list(entry.violations),
            }
            for entry in record.rounds
        ],
        "questions": [
            {
                "round": int(question.get("round") or 0),
                "role": str(question.get("role") or ""),
                "text": str(question.get("text") or ""),
                "answer": str(question.get("answer") or ""),
            }
            for question in record.questions
        ],
    }


def _require_stint(params: dict) -> tuple[Any, Any]:
    """The stint and the store holding it, so a writer writes back where it read."""
    from raven.rpc.errors import ConfigValidationError
    from raven.stint.record import mark_adrift

    stint_id = str(params.get("stint_id") or "").strip()
    if not stint_id:
        raise ConfigValidationError("stint_id is required")
    if not STINT_ID_RE.fullmatch(stint_id):
        # A stint id names a file under the store; one carrying a separator
        # reached `path_for`, which refused it as a traceback rather than an answer.
        raise ConfigValidationError(f"{stint_id!r} is not a stint id")
    stores = _stint_stores()
    mark_adrift(stores)
    for store in stores:
        record = store.read(stint_id)
        if record is not None:
            return store, record
    raise ConfigValidationError(f"no stint {stint_id} on this machine")


async def playbooks_stints_list(params: dict) -> dict:
    """Every stint, newest first.

    Stints whose holder has gone quiet are marked on the way past, so the page
    stops showing a corpse as work in progress. Marking only: taking one up
    again spends money and hours and stays a person's call.
    """
    from raven.stint.record import mark_adrift

    stores = _stint_stores()
    mark_adrift(stores)
    found = [record for store in stores for record in store.list()]
    found.sort(key=lambda record: (record.started_at_ms, record.stint_id), reverse=True)
    return {"stints": [_stint_row(record) for record in found]}


async def playbooks_stints_get(params: dict) -> dict:
    """One stint, whole."""
    return _stint_detail(_require_stint(params)[1])


async def playbooks_stints_stop(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Open no further rounds, and with ``now`` cut the round in flight short.

    Without ``now`` the round in flight is left alone: it finishes and reports,
    and the hand-over that would have opened the next one reads this and ends the
    stint instead. Throwing away a round already paid for is the worse of the two
    answers, which is why it is the verb the caller has to ask for.

    ``now`` is that ask. It reaches the round only where this process is the one
    running it -- a stint dispatched from a conversation on this gateway. A stint
    held by somebody's `raven playbook run` terminal is not addressable from
    here, and Ctrl-C there is what stops it; the record is still written either
    way, so the stint ends after the round however it was reached.
    """
    from raven.stint.record import STOPPED

    store, record = _require_stint(params)
    now = bool(params.get("now"))
    # `unfinished`, not `live`: a paused stint is not live, and it still owns its
    # branch and refuses a second stint on the project, so `stop` is the one verb
    # that has to reach it. Guarding on `live` left it with no way out at all.
    if record.unfinished:
        record.status = STOPPED
        record.stop_reason = "a person stopped the stint"
        store.write(record)
    if now:
        _cut_the_round_short(record, agent_loop_factory)
    return _stint_detail(record)


def _cut_the_round_short(record: Any, agent_loop_factory: "AgentLoopFactory | None") -> bool:
    """Signal the round in flight to stop where it is. False when out of reach.

    The same signal the graph's own cancel sends, so the round lands on the path
    that already knows what a cut round means: the roles that finished stay
    committed, the one that was cut leaves its work in the tree, and the record
    says which round was stopped before it finished.
    """
    from raven.agent.subagent.dag_live import cancel_run

    entry = record.round(record.round_index)
    if entry is None or not entry.run_id or agent_loop_factory is None:
        return False
    try:
        loop = agent_loop_factory()
    except Exception:  # noqa: BLE001 - no loop is out of reach, not a failure
        return False
    return cancel_run(loop, entry.run_id)


async def playbooks_stints_pause(params: dict) -> dict:
    """Open no further rounds, and keep the stint so it can be taken up again.

    ``stop`` and this differ in one thing and it is not what happens now: both
    let the round in flight finish and neither opens another. ``stop`` ends the
    stint, and ``pause`` leaves it unfinished, which is what ``resume`` acts on.
    So a person who is not sure they are done wants this one, and the record is
    the only place that difference is written down.

    Like ``stop``, a file write and nothing else: the process holding the round
    reads the record at the hand-over. Nothing here reaches the engine, which is
    why it can answer on a gateway that is not the one running the stint.
    """
    from raven.stint.record import PAUSED

    store, record = _require_stint(params)
    if record.live:
        record.status = PAUSED
        record.stop_reason = "a person paused the stint"
        store.write(record)
    return _stint_detail(record)


def _driver_in_this_process(agent_loop_factory: "AgentLoopFactory | None") -> Any:
    """The multi-round driver of the engine answering this call, or a refusal.

    Unlike ``pause`` and ``stop``, which write the file and let whoever holds
    the round read it, taking a stint up *opens* a round -- and a round runs
    in the process that opened it. So this has to be the engine's own driver:
    a driver built here for the call would run the round in the RPC handler
    and report to nobody.
    """
    from raven.rpc.errors import ConfigValidationError

    loop = None
    if agent_loop_factory is not None:
        try:
            loop = agent_loop_factory()
        except Exception:  # noqa: BLE001 - no loop is a refusal, not a crash
            loop = None
    runtime = getattr(loop, "_playbooks", None)
    driver = getattr(runtime, "rounds", None) if runtime is not None else None
    if driver is None:
        raise ConfigValidationError("playbooks are not running on this host, so no stint can be taken up here")
    return loop, driver


async def _take_up(params: dict, agent_loop_factory: "AgentLoopFactory | None", verb: str) -> dict:
    """``resume`` and ``extend`` share everything but the driver method they call."""
    from raven.agent import workdir
    from raven.providers.binding import use_binding

    store, record = _require_stint(params)
    loop, driver = _driver_in_this_process(agent_loop_factory)
    session_key = str(record.origin.get("session_key") or "") or None
    # The same two contexts ``playbooks.run`` enters, for the same reason: the
    # round's nodes resolve their model through the active binding, and the
    # stint was started in this conversation, not in the RPC handler's absence
    # of one. A conversation whose directory cannot be resolved still gets its
    # round -- the stint carries its own workdir -- just on the default binding.
    try:
        session_workdir = loop.session_workdir(session_key) if session_key else None
    except Exception:  # noqa: BLE001 - a bad override is not this verb's to fix
        session_workdir = None
    binding = loop.binding_for_session(session_key) if session_key else None

    async def go() -> str:
        if verb == "extend":
            return await driver.extend(record.stint_id, int(params.get("rounds") or 0), session_key)
        return await driver.resume(record.stint_id, session_key)

    if session_workdir is not None and binding is not None:
        with workdir.bind(session_workdir), use_binding(binding):
            reply = await go()
    else:
        reply = await go()
    after = store.read(record.stint_id) or record
    return {**_stint_detail(after), "reply": str(reply or "")}


async def playbooks_stints_resume(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Take a stint up again, in this engine, from the node it stopped at.

    The roles that finished are named rather than re-run, and the round reports
    to the conversation the stint was started in -- which is what a person who
    restarted raven and came back to that conversation expects, and what a
    terminal running its own driver could not give them.
    """
    return await _take_up(params, agent_loop_factory, "resume")


async def playbooks_stints_extend(params: dict, *, agent_loop_factory: "AgentLoopFactory | None" = None) -> dict:
    """Give a stint more rounds; a stint that is over opens the next one here."""
    return await _take_up(params, agent_loop_factory, "extend")


async def playbooks_stints_answer(params: dict) -> dict:
    """Answer a question a round left, for the round after this one to read."""
    import time

    from raven.rpc.errors import ConfigValidationError

    store, record = _require_stint(params)
    if params.get("question") is None:
        raise ConfigValidationError("question is required: the number `stints get` lists it under")
    position = int(params.get("question"))
    if not 0 <= position < len(record.questions):
        raise ConfigValidationError(f"{record.stint_id} has no question {position}")
    record.questions[position]["answer"] = str(params.get("text") or "")
    record.questions[position]["answered_at"] = int(time.time() * 1000)
    store.write(record)
    return _stint_detail(record)


def register_playbooks_methods(
    dispatcher: Dispatcher,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    dispatcher.register("playbooks.list", playbooks_list)
    dispatcher.register("playbooks.get", playbooks_get)
    dispatcher.register("playbooks.credentials.get", playbooks_credentials_get)
    dispatcher.register("playbooks.credentials.set", playbooks_credentials_set)
    dispatcher.register("playbooks.credentials.clear", playbooks_credentials_clear)
    dispatcher.register("playbooks.oauth.authorize", playbooks_oauth_authorize)
    dispatcher.register("playbooks.oauth.clear", playbooks_oauth_clear)
    dispatcher.register("playbooks.set_enabled", playbooks_set_enabled)

    async def _validate(p: dict) -> dict:
        return await playbooks_validate(p, agent_loop_factory=agent_loop_factory)

    dispatcher.register("playbooks.validate", _validate)
    dispatcher.register("playbooks.delete", playbooks_delete)

    async def _run(p: dict) -> dict:
        return await playbooks_run(p, agent_loop_factory=agent_loop_factory)

    dispatcher.register("playbooks.run", _run)

    async def _create(p: dict) -> dict:
        return await playbooks_create(p, agent_loop_factory=agent_loop_factory)

    dispatcher.register("playbooks.create", _create)
    dispatcher.register("playbooks.stints.list", playbooks_stints_list)
    dispatcher.register("playbooks.stints.get", playbooks_stints_get)
    async def _stop(p: dict) -> dict:
        return await playbooks_stints_stop(p, agent_loop_factory=agent_loop_factory)

    dispatcher.register("playbooks.stints.stop", _stop)
    dispatcher.register("playbooks.stints.pause", playbooks_stints_pause)

    async def _resume(p: dict) -> dict:
        return await playbooks_stints_resume(p, agent_loop_factory=agent_loop_factory)

    async def _extend(p: dict) -> dict:
        return await playbooks_stints_extend(p, agent_loop_factory=agent_loop_factory)

    dispatcher.register("playbooks.stints.resume", _resume)
    dispatcher.register("playbooks.stints.extend", _extend)
    dispatcher.register("playbooks.stints.answer", playbooks_stints_answer)


__all__ = [
    "playbooks_create",
    "playbooks_credentials_clear",
    "playbooks_credentials_get",
    "playbooks_credentials_set",
    "playbooks_get",
    "playbooks_list",
    "playbooks_oauth_authorize",
    "playbooks_oauth_clear",
    "playbooks_stints_answer",
    "playbooks_stints_get",
    "playbooks_stints_list",
    "playbooks_stints_pause",
    "playbooks_stints_stop",
    "playbooks_run",
    "register_playbooks_methods",
]
