"""A file on a registered machine, written the way scp writes one.

scp(1) (OpenSSH 9.9p2) has two forms, and only two:

- short, ``[user@]host:[path]``. It has no port field: everything after the
  first colon is the path, so ``root@203.0.113.7:49273:/a`` is a file named
  ``49273:/a``, on port 22.
- URI, ``scp://[user@]host[:port][/path]``, the one form that carries a port.
  The first slash after ``host[:port]`` only separates, so
  ``scp://h:49273/a`` is ``a`` under the login home and ``scp://h:49273//a``
  is ``/a``.

A path after the colon that does not start with ``/`` is relative to the
login home on that machine, as scp reads it.

A remote path names exactly one registered machine or none: a machine's id
names its row; an address names the row with that host and port -- the
URI's port, else 22 -- and never the one a local ssh config would choose,
so one path means one machine on every computer. A user written in the path
must be the row's. Anything else is refused before any ssh, with the way a
local file whose name holds a colon is written instead (``./a:b``).

Shown, a remote path uses the registry's own user, host and port, in the
form scp reads back: ``user@host:/abs/path`` on port 22, else
``scp://user@host:port//abs/path`` -- the port only fits the URI form.

Reads the registry, and for :func:`reading` and :func:`writing` the agent
entries' ``remoteCwd``; never ssh, never a node. Remote spellings are always
resolved against the registry; an explicit local spelling such as ``./a:b``
stays on this computer.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Where an agent works on its machine when its entry names nowhere else, and
#: so the one directory every registered machine reads from without asking.
#: ``~`` is the registered user's home there, expanded by the machine.
DEFAULT_ROOT = "~/raven-work"

#: The reading tools a remote path reaches, and so the ones its permission
#: check is about.
READING_TOOLS = frozenset({"read_file", "list_dir", "find", "grep"})

#: The writing tools a remote path reaches. Asked about wherever they fall, as
#: a write here is; the roots place their fence, not their answer.
WRITING_TOOLS = frozenset({"write_file", "edit_file"})

# The tools whose path names a file, so the directory a call reaches is the file's.
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})

#: The URI form's scheme. ``sftp://`` reads the same way, and is accepted as
#: one more spelling of it.
_URI = re.compile(r"^(?:scp|sftp)://", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_DRIVE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")
#: A drive-relative path (``C:file``), which only a Windows host reads as one: elsewhere ``c:file`` is scp's machine ``c``.
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:")
_WINDOWS = os.name == "nt"
_PORT = re.compile(r"^[0-9]{1,5}$")


class RemotePathError(ValueError):
    """A path that looks remote but names no registered machine. The message names no address it did not get."""


class NodeCallError(RuntimeError):
    """A file call a machine's Raven node could not answer (``raven.node.client.NodeError``). Safe to show.

    ``reason`` names the node's own refusal when it gave one -- ``changed``
    (a write the file's change forbade), ``corrupt``, ``not-found``, ``busy``
    -- so a caller can say the right sentence for a refusal it expects. A
    name rather than the node's code, so the file tools need not import the
    node's protocol: the node imports them.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


# How a file call reaches a machine's Raven node: the process-wide pool,
# handed over at assembly (``raven.core.runtime``). Kept here, in the registry's
# package, so the file tools reach the node without importing the node package,
# which imports them.
_NODES: Callable[[], Any] | None = None


def use_nodes(get_pool: Callable[[], Any]) -> None:
    """Hand over how this process reaches a machine's node: ``raven.node.client.get_node_pool``."""
    global _NODES
    _NODES = get_pool


def nodes() -> Any:
    """This process's node pool, or ``None`` when none was handed over (a process that reads no machine)."""
    return _NODES() if _NODES is not None else None


@dataclass(frozen=True)
class RemotePath:
    """A path on one registered machine."""

    machine: str
    """The machine's id in the registry."""
    path: str
    """The path on the machine: absolute, or relative to the login home (``""`` is the home itself)."""
    _row: dict[str, Any] = field(repr=False, compare=False)

    @property
    def is_absolute(self) -> bool:
        return self.path.startswith("/")

    def absolute(self, home: str) -> str:
        """This path as the machine names it, given the login ``home`` there; ``..`` folded away.

        Only the login user's own ``~`` is expanded here; ``~other`` is left
        to the machine, and stays relative to this check.
        """
        return absolute(self.path, home)

    def display(self, path: str | None = None) -> str:
        """``path`` (default: this one) on this machine, the way scp writes it back.

        Port 22 gives the short form; any other port the URI form, the only
        one that can say it. A relative path stays relative, as scp reads it.
        """
        where = self.path if path is None else path
        user = str(self._row.get("user") or "root")
        host = str(self._row.get("host") or "").strip()
        if ":" in host:
            host = f"[{host}]"
        port = int(self._row.get("port") or 22)
        if port == 22:
            return f"{user}@{host}:{where}"
        return f"scp://{user}@{host}:{port}/{where}"


def absolute(path: str, home: str) -> str:
    """``path`` on a machine whose login home is ``home``, as an absolute path with ``..`` folded away."""
    raw = str(path or "").strip()
    if raw in ("", "~"):
        return posixpath.normpath(home) if home else ""
    if raw.startswith("~/"):
        raw = raw[2:]
    elif raw.startswith("~"):
        return raw
    if raw.startswith("/"):
        return posixpath.normpath(raw)
    return posixpath.normpath(posixpath.join(home, raw)) if home else raw


def within(path: str, root: str) -> bool:
    """Whether the absolute ``path`` is ``root`` or under it, compared as text after folding ``..``."""
    if not path.startswith("/") or not root.startswith("/"):
        return False
    path, root = posixpath.normpath(path), posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


# The login home on each machine, as its Raven node reported it. Learned when a
# node starts (``raven.node.client``) and kept for the life of the process, so
# a permission check can place a ``~`` path without opening ssh to ask.
_HOMES: dict[str, str] = {}


def remember_home(machine: str, home: str) -> None:
    """Record ``home`` as the login home on ``machine``."""
    if machine and home.startswith("/"):
        _HOMES[machine] = posixpath.normpath(home)


def known_home(machine: str) -> str:
    """The login home on ``machine`` if a node has reported it in this process, else ``""``."""
    return _HOMES.get(machine, "")


# The digest of each remote file as its reader last read it in this process,
# by reader, machine and absolute path (``raven.agent.tools.remote_files``), so
# a write that replaces the whole file can say what it expects to replace. By
# reader because the digest describes what one agent saw: another agent's
# later read must not vouch for a write based on an older one. Bounded, since
# readers come and go; past the bound the oldest entry goes, and the write it
# would have checked is then one by an agent that never read the file.
_READ: dict[tuple[str, str, str], str] = {}
_READ_LIMIT = 4096


def remember_read(machine: str, path: str, digest: str, *, reader: str = "") -> None:
    """Record ``digest`` as what ``path`` on ``machine`` held when ``reader`` read it."""
    if machine and path.startswith("/") and digest:
        _READ.pop((reader, machine, path), None)
        _READ[(reader, machine, path)] = digest
        while len(_READ) > _READ_LIMIT:
            del _READ[next(iter(_READ))]


def last_read(machine: str, path: str, *, reader: str = "") -> str | None:
    """The digest ``path`` on ``machine`` had when ``reader`` last read it in this process, if it did."""
    return _READ.get((reader, machine, path))


def forget_read(machine: str, path: str, *, reader: str = "") -> None:
    """Drop what ``reader`` read of ``path`` on ``machine``: it wrote the file, or found it changed."""
    _READ.pop((reader, machine, path), None)


def target(text: str) -> RemotePath | None:
    """:func:`parse` against the registry as it is now; ``None`` for a local path."""
    if not looks_remote(text):
        return None
    from raven.ops.connections import load

    return parse(text, load())


def reading_roots(where: RemotePath) -> list[str]:
    """The directories on ``where``'s machine a read needs no question for.

    The default remote root, the ``remoteCwd`` of every acp agent entry that
    names this machine, and the registry row's ``paths``: where this Raven's
    agents work there and where the owner said its work lives. As written,
    ``~`` included; :func:`absolute` places them.
    """
    roots = [DEFAULT_ROOT]
    try:
        from raven.config.loader import load_config

        entries = load_config().subagents.agents
    except Exception:  # noqa: BLE001 -- a config that cannot load names no extra root
        entries = []
    for entry in entries:
        if str(getattr(entry, "machine", None) or "").strip() == where.machine:
            roots.append(str(getattr(entry, "remote_cwd", None) or "").strip() or DEFAULT_ROOT)
    paths = where._row.get("paths")
    if isinstance(paths, list):
        roots += [str(p) for p in paths if str(p).startswith("/")]
    return list(dict.fromkeys(roots))


def _placed(path: str, home: str) -> str:
    """``path`` absolute when the home is known; else in its ``~/`` spelling, which compares as text."""
    if home or path.startswith("/"):
        return absolute(path, home)
    raw = path.strip()
    if raw in ("", "~"):
        return "~"
    rest = raw[2:] if raw.startswith("~/") else raw
    return "~/" + posixpath.normpath(rest).lstrip("/") if not rest.startswith("~") else raw


def _under(path: str, root: str) -> bool:
    if path.startswith("/") and root.startswith("/"):
        return within(path, root)
    if path.startswith("~") and root.startswith("~"):
        return path == root or path.startswith(root.rstrip("/") + "/")
    return False


@dataclass(frozen=True)
class Reading:
    """What one call on a remote path reaches, for its permission check."""

    target: RemotePath
    place: str
    """The path the call names, placed on the machine (:func:`_placed`)."""
    directory: str
    """The directory the call reaches: the file's for a tool that names a file, else the path itself."""
    inside: bool
    """Whether that is under a :func:`reading_roots` directory, so a read asks no question."""


def reading(tool: str, params: dict[str, Any] | None) -> Reading | None:
    """What ``tool``'s reading call reaches on a registered machine, or ``None``.

    ``None`` for any other tool, a local path, and a path that names no
    machine -- that call reaches nothing and the tool says why. Placed with
    the login home a node has reported in this process, when one has; else
    compared as written, which can only ask where it might not have.
    """
    return _reach(tool, params) if tool in READING_TOOLS else None


def writing(tool: str, params: dict[str, Any] | None) -> Reading | None:
    """What ``tool``'s writing call reaches on a registered machine, or ``None``.

    A write there asks wherever it falls, as a write here does
    (``raven.permissions.rules.default_tier``), and a grant is for the one
    file, apart from any read that was allowed
    (``raven.permissions.builtin.session_keys``); the roots decide only the
    fence the node is given (:func:`fence`).
    """
    return _reach(tool, params) if tool in WRITING_TOOLS else None


def _reach(tool: str, params: dict[str, Any] | None) -> Reading | None:
    path = (params or {}).get("path")
    if not isinstance(path, str):
        return None
    try:
        where = target(path)
    except RemotePathError:
        return None
    if where is None:
        return None
    home = known_home(where.machine)
    place = _placed(where.path, home)
    directory = posixpath.dirname(place) or place if tool in _FILE_TOOLS else place
    inside = any(_under(place, _placed(root, home)) for root in reading_roots(where))
    return Reading(where, place, directory, inside)


def fence(tool: str, where: RemotePath, place: str, home: str) -> str:
    """The one directory a node may touch for this call: the root it is under, else what was approved.

    ``place`` is the call's absolute path on the machine. Under a
    :func:`reading_roots` directory the fence is that root, so a link
    between two directories inside it still reads; otherwise it is the
    directory the permission check asked about, and nothing beyond it.
    """
    for root in reading_roots(where):
        placed = absolute(root, home)
        if within(place, placed):
            return placed
    return (posixpath.dirname(place) or "/") if tool in _FILE_TOOLS else place


def looks_remote(text: str) -> bool:
    """Whether ``text`` is written as a remote path, before asking which machine it names.

    Local, as scp reads it: anything starting with ``/``, ``./``, ``../`` or
    ``~``; a Windows drive (``C:\\`` or ``C:/``), a UNC or extended Windows
    path (``\\\\server\\share``, ``\\\\?\\C:\\...``) and, on a Windows host,
    a drive-relative path (``C:file``); a URL of another scheme; and any
    path whose first ``/`` comes before its first ``:``.
    """
    raw = str(text or "").strip()
    if not raw or raw.startswith(("/", "./", "../", "~", "\\\\")) or _DRIVE.match(raw):
        return False
    if _WINDOWS and _DRIVE_RELATIVE.match(raw):
        return False
    if _URI.match(raw):
        return True
    if _SCHEME.match(raw):
        return False
    colon = raw.find(":")
    if colon <= 0:
        return False
    slash = raw.find("/")
    return slash == -1 or colon < slash


def parse(text: str, rows: list[dict[str, Any]]) -> RemotePath | None:
    """The remote path ``text`` names, or ``None`` when it is a local path.

    ``rows`` is the registry (``raven.ops.connections.load()``). Raises
    :class:`RemotePathError` for a path written as remote that names no
    registered machine, or names one with another user or port.
    """
    raw = str(text or "").strip()
    if not looks_remote(raw):
        return None
    if _URI.match(raw):
        user, host, port, path = _split_uri(raw)
    else:
        user, host, path = _split_short(raw)
        port = None
    row = _row_for(host, port, rows, raw)
    registered_user = str(row.get("user") or "root")
    if user is not None and user != registered_user:
        raise RemotePathError(
            f"{raw!r} logs in as {user!r}, and machine {_label(row)} is registered for another user; "
            "write the path without the user, or with the registered one"
        )
    return RemotePath(machine=str(row.get("id")).strip(), path=path, _row=dict(row))


def _split_uri(raw: str) -> tuple[str | None, str, int | None, str]:
    rest = _URI.sub("", raw, count=1)
    authority, slash, path = rest.partition("/")
    user, _, hostport = authority.rpartition("@") if "@" in authority else (None, "", authority)
    host, port = _host_port(hostport, raw)
    # The first slash only separated; what follows is relative to the home
    # unless it starts with another slash (``scp://h//abs``).
    return (user or None), host, port, (path if slash else "")


def _split_short(raw: str) -> tuple[str | None, str, str]:
    if "@[" in raw or raw.startswith("["):
        close = raw.find("]")
        if close == -1 or raw[close + 1 : close + 2] != ":":
            raise RemotePathError(f"{raw!r} opens an address with '[' and does not close it before the ':'")
        head, path = raw[: close + 1], raw[close + 2 :]
    else:
        head, _, path = raw.partition(":")
    user, _, host = head.rpartition("@") if "@" in head else (None, "", head)
    return (user or None), host.strip("[]"), path


def _host_port(hostport: str, raw: str) -> tuple[str, int | None]:
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1:
            raise RemotePathError(f"{raw!r} opens an address with '[' and does not close it")
        host, tail = hostport[1:close], hostport[close + 1 :]
        if not tail:
            return host, None
        if not tail.startswith(":"):
            raise RemotePathError(f"{raw!r} has something after the address that is not a port")
        port_text = tail[1:]
    else:
        host, sep, port_text = hostport.partition(":")
        if not sep:
            return host, None
    if not _PORT.match(port_text) or not 0 < int(port_text) < 65536:
        raise RemotePathError(f"{raw!r} names port {port_text!r}, which is not a port")
    return host, int(port_text)


def _row_for(host: str, port: int | None, rows: list[dict[str, Any]], raw: str) -> dict[str, Any]:
    wanted = host.strip()
    if not wanted:
        raise RemotePathError(_unknown(raw))
    by_id = [r for r in rows if str(r.get("id") or "").strip() == wanted]
    if by_id:
        row = by_id[0]
        if port is not None and port != int(row.get("port") or 22):
            raise RemotePathError(
                f"{raw!r} names machine {_label(row)} on a port it is not registered on; "
                "write the path without the port"
            )
        return row
    address = wanted.lower()
    reach = 22 if port is None else port
    hits = [
        r for r in rows if str(r.get("host") or "").strip().lower() == address and int(r.get("port") or 22) == reach
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        names = ", ".join(_label(r) for r in hits)
        raise RemotePathError(f"{raw!r} matches more than one registered machine ({names}); write the machine's id")
    raise RemotePathError(_unknown(raw))


def _unknown(raw: str) -> str:
    return (
        f"{raw!r} is written as a path on another machine, and no registered machine matches it "
        "(by its id, or by its address and port: the port written after the address in scp://, else 22). "
        "For a file on this computer whose name holds a colon, write it as ./name or as an absolute path"
    )


def _label(row: dict[str, Any]) -> str:
    conn_id = str(row.get("id") or "").strip()
    name = str(row.get("display_name") or "").strip()
    return f"{name!r} ({conn_id})" if name and name != conn_id else repr(conn_id)


__all__ = [
    "DEFAULT_ROOT",
    "READING_TOOLS",
    "WRITING_TOOLS",
    "NodeCallError",
    "Reading",
    "RemotePath",
    "RemotePathError",
    "absolute",
    "fence",
    "forget_read",
    "known_home",
    "last_read",
    "looks_remote",
    "nodes",
    "parse",
    "reading",
    "reading_roots",
    "remember_home",
    "remember_read",
    "target",
    "use_nodes",
    "within",
    "writing",
]
