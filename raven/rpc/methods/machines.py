"""``machines.*`` -- the Agent Hub's machines tab over the machine registry.

The registry (:mod:`raven.ops.connections`) is what ``raven ops connection``
keeps: the machines the owner named, with the way onto each. This group is
that command behind a page -- list, add, install-node, remove, doctor --
answering what the command prints, as data, with the one difference the
contract states: nothing here carries an address, an account or a key path.
A row says what a machine is, how this computer's link to it is doing
(:meth:`raven.node.client.NodePool.status`) and whether its Raven node is
this Raven's own code; the way onto it travels once, in ``machines.add``, and
is not read back.

An install takes minutes and a page cannot hold a call that long, so
``machines.install_node`` starts one in the background and ``machines.list``
reports it (``install`` on the row) until the next one on that machine. The
work is :mod:`raven.node.install`'s, run in a thread: each of its steps is an
ssh call, and the gateway's loop keeps answering meanwhile.

What a machine's node is, when no link is up, is read off the registry row:
``node_install`` is the install name (``<version>-<digest>``) recorded there by
the last install or contact from this computer (the command records it too).
A link that is up is a node of this Raven's code -- the pool admits no other
-- so the live version wins while there is one.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import ValidationError

from raven.acp_client import remote
from raven.config.update_subagents import agents_on_machine
from raven.node import bundle
from raven.node import install as node_install
from raven.node.client import MachineStatus, NodeError, get_node_pool
from raven.ops import connection_add, connections
from raven.ops.connection_add import NODE_FIELD
from raven.ops.connections import SSH, UNREADABLE, password_auth, row_problems, transport_of
from raven.rpc.errors import ConfigValidationError

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


@dataclass
class _Install:
    """One node install asked for from the page, as the row reports it until the next."""

    running: bool = True
    ok: bool | None = None
    detail: str = ""
    task: asyncio.Task[None] | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"running": self.running, "detail": self.detail}
        if self.ok is not None:
            out["ok"] = self.ok
        return out


# machine id -> its last install from this process. Kept after the end so the
# page can read how it ended; replaced by the next install on that machine.
_INSTALLS: dict[str, _Install] = {}

_NO_INSTALL = {"running": False, "detail": ""}


def _label(row: dict[str, Any]) -> str:
    """How a sentence names the machine: the owner's name for it, then the id (as ``remote.Machine.label``)."""
    conn_id = str(row.get("id") or "")
    name = str(row.get("display_name") or "")
    return f"{name!r} ({conn_id})" if name and name != conn_id else repr(conn_id)


def _scrubbed(text: str, row: dict[str, Any]) -> str:
    """``text`` with the row's way in taken out: ssh's own words name the address, a probe names the login."""
    host = str(row.get("host") or "")
    user = str(row.get("user") or "")
    key = str(row.get("key") or "")
    port = str(row.get("port") or "")
    out = text
    if host:
        if user and port:
            out = out.replace(f"{user}@{host}:{port}", "the machine")
        if user:
            out = out.replace(f"{user}@{host}", "the machine")
        if port:
            out = out.replace(f"{host}:{port}", "the machine")
        out = out.replace(host, "the machine")
        # ssh names the login twice ("could not reach user@host:port: user@host: Permission denied"); once is enough.
        out = out.replace("the machine: the machine:", "the machine:")
    if key:
        out = out.replace(key, "the key")
    return out


def _node_of(row: dict[str, Any], status: Any) -> tuple[str | None, bool]:
    """``(version, current)`` for the row: the live node while a link is up, else the recorded one."""
    if status.state in ("connected", "idle") and status.version:
        return str(status.version), True
    recorded = str(row.get(NODE_FIELD) or "").strip()
    if recorded:
        return recorded.rpartition("-")[0] or recorded, recorded == bundle.install_name()
    return None, False


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    conn_id = str(row.get("id") or "").strip()
    status = get_node_pool().status(conn_id) if transport_of(row) == SSH else MachineStatus("off")
    version, current = _node_of(row, status)
    install = _INSTALLS.get(conn_id)
    out: dict[str, Any] = {
        "id": conn_id,
        "name": str(row.get("display_name") or conn_id),
        "transport": transport_of(row),
        "state": status.state,
        "detail": _scrubbed(status.detail, row),
        "agents": status.agents,
        "node_current": current,
        "install": install.as_json() if install is not None else dict(_NO_INSTALL),
        "paths": [str(x) for x in row["paths"]] if isinstance(row.get("paths"), list) else [],
        "problems": [p.text for p in row_problems(row) if p.blocking],
    }
    if version:
        out["node_version"] = version
    if transport_of(row) == SSH:
        out["auth"] = "password" if password_auth(row) else "key"
    return out


def _registry() -> connections.Read:
    found = connections.read()
    if found.state == UNREADABLE:
        raise ConfigValidationError(found.detail)
    return found


def _record(conn_id: str, fields: dict[str, Any]) -> None:
    """Keep what was learned about the machine on its row; a row that cannot be changed is logged, not fatal."""
    try:
        connection_add.set_fields(conn_id, fields)
    except ValueError as exc:
        logger.warning("machine {}: {} not recorded: {}", conn_id, ", ".join(fields), exc)


async def _reach(row: dict[str, Any]) -> None:
    """Bring the link to the machine up and ask its node who it is; the pool keeps the refusal for the listing."""
    conn_id = str(row.get("id") or "").strip()
    try:
        hello = await get_node_pool().hello(conn_id)
    except (NodeError, remote.RemoteMachineError, OSError) as exc:
        logger.info("machine {}: not reached: {}", conn_id, exc)
        return
    name = f"{hello.get('version') or ''}-{hello.get('digest') or ''}".strip("-")
    if name and name != str(row.get(NODE_FIELD) or ""):
        _record(conn_id, {NODE_FIELD: name})


async def machines_list(params: dict) -> dict:
    """Every registered machine with the state of its link and node; ``refresh`` reaches them first."""
    only = str(params.get("id") or "").strip()
    found = _registry()
    if params.get("refresh"):
        rows = [
            r
            for r in found.rows
            if transport_of(r) == SSH
            and (not only or str(r.get("id") or "").strip() == only)
            and connections.usable([r])
        ]
        await asyncio.gather(*(_reach(r) for r in rows))
        found = _registry()
    return {"rows": [_row_out(r) for r in found.rows]}


# Why a machine was not registered, as a kind the page explains in the reader's
# language; the sentence itself stays, for the reader who wants ssh's words.
_REASONS = (
    (re.compile(r"ssh(-keygen)? not found on this computer", re.IGNORECASE), "nossh"),
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed", re.IGNORECASE), "hostkey"),
    (re.compile(r"Permission denied|Too many authentication failures|publickey", re.IGNORECASE), "login"),
    (re.compile(r"no answer within|timed out", re.IGNORECASE), "timeout"),
)


def _reason(message: str) -> str:
    for pattern, reason in _REASONS:
        if pattern.search(message):
            return reason
    return "unreachable"


def _refused(detail: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "detail": detail, "reason": reason}


async def machines_add(params: dict) -> dict:
    """Reach an ssh machine once with the given login, then write its row and its ssh alias.

    With ``auth: password`` the password is what Raven logs in with, every
    time: kept on this computer for the owner alone
    (``connection_add.store_password``) before the probe, which then reaches
    the machine the way every later connection will, through ssh's askpass
    hook; a password the machine refuses is forgotten again. It is in no
    registry row, log or answer. Such a row gets no ssh alias. On Windows the
    pick itself is refused, first of all, before anything is kept.
    """
    conn_id = str(params.get("id") or "").strip()
    name = str(params.get("name") or "").strip()
    paths = [str(x).strip() for x in (params.get("paths") or []) if str(x).strip()]
    by_password = str(params.get("auth") or "key").strip().lower() == "password"
    password = str(params.get("password") or "")
    row: dict[str, Any] = {
        "id": conn_id,
        "display_name": name or conn_id,
        "transport": SSH,
        "host": str(params.get("host") or "").strip(),
        "port": params.get("port") or 22,
        "user": str(params.get("user") or "").strip(),
        "key": str(params.get("key") or "").strip(),
    }
    if paths:
        row["paths"] = paths
    if by_password:
        if sys.platform == "win32":
            # Judged before anything else the pick would set in motion: every
            # password login rides OpenSSH's askpass, and the helper Raven
            # writes is a POSIX script this computer cannot run, so nothing is
            # kept and no 'nossh' asks for an OpenSSH that could not make the
            # mode work either.
            return _refused("registering with a password is not supported on Windows; use a key instead", "platform")
        if not password:
            return _refused("registering with a password needs the password; nothing was written", "invalid")
        row["auth"] = "password"
        del row["key"]
    faults = [p.text for p in row_problems(row) if p.blocking]
    if faults:
        return _refused("; ".join(faults), "invalid")
    found = connections.read()
    if found.state == UNREADABLE:
        return _refused(found.detail, "registry")
    if found.detail:
        # Judged before the machine is reached: the write would refuse anyway
        # (``connection_add.write``), and the probe is a round trip to a
        # machine the owner then has to be asked about again.
        return _refused(f"{found.detail} Give those entries an id (or remove them) first.", "registry")
    if any(str(r.get("id") or "").strip() == conn_id for r in found.rows):
        return _refused(f"a machine with id {conn_id!r} is registered already", "duplicate")
    if by_password:
        # Kept before the probe, which reaches the machine the way every later
        # connection will: through the askpass helper reading this file.
        connection_add.store_password(conn_id, password)
    reached, message, detected = await asyncio.to_thread(connection_add.probe, row)
    if not reached:
        said = _scrubbed(message, row)
        if by_password:
            # A password the machine refused is not kept; and ssh never echoes
            # one, but the replace is the guarantee rather than the belief.
            connection_add.forget_password(conn_id)
            said = said.replace(password, "the password")
        return _refused(said, _reason(message))
    row.update(detected)
    row["software"] = ""
    row["budget_unit"] = "gpu-minute" if row.get("kind") == "gpu" else "core-minute"
    if not connections.resource_unit(row):
        row["concurrency"] = 1
    try:
        connection_add.write(row)
    except ValueError as exc:
        said = str(exc)
        return _refused(said, "duplicate" if "already listed" in said else "registry")
    said = [f"{detected['cores']} cores" if "cores" in detected else "", str(detected.get("memory") or "")]
    said.append(str(detected.get("device") or ""))
    detail = f"reached machine {_label(row)}: " + (
        ", ".join(x for x in said if x) or "nothing it could say about itself"
    )
    try:
        connection_add.write_ssh_alias(row)
    except (OSError, ValueError) as exc:
        detail += f"; its ssh alias was not written to ~/.ssh/config ({exc}), so scp and rsync cannot address it by id"
    return {"ok": True, "detail": detail, "reason": "", "row": _row_out(row)}


def _forget_password(row: dict[str, Any], conn_id: str) -> str:
    """For a row registered with a password: the password kept here goes with the row; one clause, or none."""
    if not password_auth(row):
        return ""
    connection_add.forget_password(conn_id)
    return "; the password kept here for it is deleted"


async def _install(conn_id: str, node_dir: str, rec: _Install) -> None:
    """The install itself, in the background: look, refuse or install, record, and hear the node answer."""
    try:
        plan = await asyncio.to_thread(node_install.plan, conn_id, node_dir=node_dir or None)
        if plan.present:
            rec.detail = (
                f"Machine {plan.target.label} already has the Raven node {plan.install_name} in {plan.node_dir}."
            )
        else:
            if (why := node_install.refusal(plan)) is not None:
                raise node_install.InstallError(why)
            rec.detail = plan.describe()
            done = await asyncio.to_thread(node_install.install, plan)
            size = f" ({done.size_kb // 1024} MB)" if done.size_kb else ""
            rec.detail = f"Installed the Raven node {done.install_name} in {done.node_dir}{size}."
            if not done.rg:
                rec.detail += " ripgrep-bin was not available there; grep on it runs Raven's own Python search."
        learned: dict[str, Any] = {NODE_FIELD: plan.install_name}
        if node_dir and node_dir != str(plan.target.field("node_dir") or "").strip().rstrip("/"):
            learned["node_dir"] = node_dir
        _record(conn_id, learned)
        hello = await get_node_pool().hello(conn_id)
        rec.detail += f" The node answers: Python {hello.get('python')}."
        rec.ok = True
    except (node_install.InstallError, NodeError, remote.RemoteMachineError) as exc:
        rec.ok = False
        rec.detail = str(exc)
    except asyncio.CancelledError:
        rec.ok = False
        rec.detail = "the install was interrupted before it finished"
        raise
    except Exception as exc:  # noqa: BLE001 -- the page must hear the end, whatever ended it
        logger.exception("installing the Raven node on machine {} failed", conn_id)
        rec.ok = False
        rec.detail = f"installing the Raven node on machine {conn_id!r} failed: {exc}"
    finally:
        rec.running = False


async def machines_install_node(params: dict) -> dict:
    """Put this Raven's node on a registered machine, in the background; the list reports the rest."""
    conn_id = str(params.get("id") or "").strip()
    node_dir = str(params.get("node_dir") or "").strip().rstrip("/")
    running = _INSTALLS.get(conn_id)
    if running is not None and running.running:
        return {"started": False, "detail": f"an install is already running on machine {conn_id!r}"}
    try:
        target = remote.machine(conn_id)
    except remote.RemoteMachineError as exc:
        return {"started": False, "detail": str(exc)}
    rec = _Install(detail=f"looking at machine {target.label}")
    rec.task = asyncio.create_task(_install(conn_id, node_dir, rec))
    _INSTALLS[conn_id] = rec
    return {"started": True, "detail": f"installing the Raven node on machine {target.label}"}


async def machines_doctor(params: dict) -> dict:
    """What the registry's doctor says: every problem, blocking or not, with the row it is about."""
    ids = {str(r.get("id") or "").strip() for r in connections.load()}
    out = []
    for problem in connections.problems():
        head, sep, _ = problem.text.partition(": ")
        out.append({"id": head if sep and head in ids else "", "blocking": problem.blocking, "detail": problem.text})
    return {"problems": out}


def _not_removed(detail: str, agents: list[str]) -> dict[str, Any]:
    return {"removed": False, "detail": detail, "agents": agents}


async def machines_remove(params: dict) -> dict:
    """Take a machine out of the registry, unless agent entries name it or agents run on it (``force`` overrides)."""
    conn_id = str(params.get("id") or "").strip()
    force = bool(params.get("force"))
    found = connections.read()
    if found.state == UNREADABLE:
        return _not_removed(found.detail, [])
    rows = [r for r in found.rows if str(r.get("id") or "").strip() == conn_id]
    if not rows:
        return _not_removed(f"machine {conn_id!r} is not registered", [])
    label = _label(rows[0])
    try:
        named = agents_on_machine(conn_id, only_enabled=True)
        left = [name for name in agents_on_machine(conn_id) if name not in named]
    except ValidationError:
        # A malformed agents section is the agents page's to report; it
        # cannot name an entry that would fail, so nothing is refused over it.
        named, left = [], []
    if named and not force:
        n = len(named)
        return _not_removed(
            f"{n} connected agent entr{'y names' if n == 1 else 'ies name'} machine {label} ({', '.join(named)}); "
            "disconnect them first",
            named,
        )
    pool = get_node_pool()
    live = pool.status(conn_id).agents if transport_of(rows[0]) == SSH else 0
    if live and not force:
        return _not_removed(
            f"{live} agent{'s are' if live != 1 else ' is'} running on machine {label} right now; "
            "wait for them to finish or stop them first",
            named,
        )
    try:
        connection_add.remove(conn_id)
    except ValueError as exc:
        return _not_removed(str(exc), named)
    detail = f"removed machine {label} from the registry"
    try:
        if connection_add.remove_ssh_alias(conn_id):
            detail += "; its ssh alias is gone from ~/.ssh/config"
    except (OSError, ValueError) as exc:
        detail += f"; its ssh alias could not be removed from ~/.ssh/config ({exc})"
    if transport_of(rows[0]) == SSH:
        await pool.close(conn_id)
        detail += "; the node installed on it stays there"
        detail += _forget_password(rows[0], conn_id)
    _INSTALLS.pop(conn_id, None)
    if named:
        detail += (
            f"; {', '.join(named)} still name{'s' if len(named) == 1 else ''} it and fail{'s' if len(named) == 1 else ''}"
            " at dispatch until disconnected"
        )
    if left:
        detail += (
            f"; the disconnected entr{'y' if len(left) == 1 else 'ies'} {', '.join(left)} still name{'s' if len(left) == 1 else ''}"
            " it and cannot be connected until it is registered again"
        )
    return {"removed": True, "detail": detail, "agents": named + left}


def register_machines_methods(dispatcher: "Dispatcher") -> None:
    """Register the ``machines.*`` methods on a dispatcher instance."""
    dispatcher.register("machines.list", machines_list)
    dispatcher.register("machines.add", machines_add)
    dispatcher.register("machines.install_node", machines_install_node)
    dispatcher.register("machines.doctor", machines_doctor)
    dispatcher.register("machines.remove", machines_remove)


__all__ = [
    "NODE_FIELD",
    "machines_add",
    "machines_doctor",
    "machines_install_node",
    "machines_list",
    "machines_remove",
    "register_machines_methods",
]
