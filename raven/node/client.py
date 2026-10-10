"""The host's side of a Raven node: one live node per registered machine.

A node is started over ssh with the launch line every agent on a machine gets
(:mod:`raven.acp_client.remote`): the registry's address, port and key, the
machine user's login shell, ssh's own words kept in their log. It is spoken to
with the client an ACP agent is (:class:`raven.acp_client.client.AcpClient`),
because the framing is the same. One node serves every file call to its
machine; one that has sat idle past :data:`IDLE_CLOSE_S` is closed the next
time any node is asked for, and its ssh goes with it.

Before a node is used, ``node/hello`` has to show the same code as this Raven
(:func:`raven.node.bundle.digest`): a node that differs would answer a file call
differently from the tool it stands in for. Every failure here is one sentence
that names the machine, never its address, and says what to do.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from raven.acp_client import remote
from raven.acp_client.client import AcpClient
from raven.acp_client.protocol import AcpConnectionError, AcpError, AcpRemoteError, AcpTimeoutError
from raven.node import bundle, protocol

#: Where a machine keeps its nodes when its registry row names nowhere else
#: (``node_dir``). ``~`` is the registered user's home there.
DEFAULT_NODE_DIR = "~/.raven-node"

#: How long a node may sit unused before the next acquire closes it.
IDLE_CLOSE_S = 600.0

_HELLO_TIMEOUT_S = 60.0
_CALL_TIMEOUT_S = 120.0

#: How the machine's shell ends when the node's Python is not there: 127 from
#: dash and bash 5, 126 from bash 3.2 (macOS's ``/bin/sh``), which reports an
#: ``exec`` of a missing path as "cannot execute" (measured 2026-10-10).
_MISSING_RCS = frozenset({remote.NOT_FOUND_RC, 126})


class NodeError(RuntimeError):
    """A file call to a machine that could not be made. The message is safe to show."""


def node_dir(target: remote.Machine) -> str:
    """The directory on ``target`` that holds its nodes."""
    return str(target.field("node_dir") or "").strip().rstrip("/") or DEFAULT_NODE_DIR


def node_python(target: remote.Machine, install_name: str) -> str:
    """The Python of the node of ``install_name`` on ``target``."""
    return f"{node_dir(target)}/{install_name}/bin/python"


def install_hint(target: remote.Machine) -> str:
    """The command that puts this Raven's node on ``target``."""
    return f"raven ops connection install-node {target.id}"


@dataclass
class _Node:
    client: AcpClient
    launched: remote.RemoteLaunch
    used: float = field(default_factory=time.monotonic)


class NodePool:
    """Live nodes, at most one per machine, shared by every caller in this process."""

    def __init__(self, *, idle_close_s: float = IDLE_CLOSE_S) -> None:
        self._nodes: dict[str, _Node] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._idle_close_s = idle_close_s

    async def call(
        self, machine_id: str, method: str, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT_S
    ) -> dict[str, Any]:
        """``method`` on the node of ``machine_id``, starting one if none is live."""
        node = await self._acquire(machine_id)
        label = node.launched.target.label
        try:
            result = await node.client.request(method, params, timeout=timeout)
        except AcpRemoteError as exc:
            raise NodeError(f"the Raven node on machine {label} refused {method}: {exc.message}") from None
        except AcpTimeoutError:
            await self._drop(machine_id)
            raise NodeError(
                f"the Raven node on machine {label} did not answer {method} within {timeout:.0f}s"
            ) from None
        except AcpConnectionError as exc:
            await self._drop(machine_id)
            raise NodeError(self._ended(node.launched, exc)) from None
        node.used = time.monotonic()
        return result if isinstance(result, dict) else {}

    async def hello(self, machine_id: str) -> dict[str, Any]:
        """The handshake of the node of ``machine_id``: its protocol, digest, Python and home."""
        return await self.call(machine_id, protocol.HELLO, {"protocol": protocol.PROTOCOL})

    def live(self) -> list[str]:
        """The machines that have a node running for this process."""
        return [machine_id for machine_id, node in self._nodes.items() if node.client.alive]

    async def close(self, machine_id: str) -> None:
        await self._drop(machine_id)

    async def close_all(self) -> None:
        for machine_id in list(self._nodes):
            await self._drop(machine_id)

    async def _acquire(self, machine_id: str) -> _Node:
        await self._close_idle(keep=machine_id)
        lock = self._locks.setdefault(machine_id, asyncio.Lock())
        async with lock:
            node = self._nodes.get(machine_id)
            if node is not None and node.client.alive:
                return node
            if node is not None:
                await self._drop(machine_id)
            node = await self._launch(machine_id)
            self._nodes[machine_id] = node
            return node

    async def _launch(self, machine_id: str) -> _Node:
        try:
            target = await asyncio.to_thread(remote.machine, machine_id)
        except remote.RemoteMachineError as exc:
            raise NodeError(str(exc)) from None
        install_name = bundle.install_name()
        command = f"{remote.shell_path(node_python(target, install_name))} -m raven.node --stdio"
        try:
            # Started from the home directory: the node dir may be on a full disk
            # where the launch line's ``mkdir -p`` of a fresh root would fail.
            launched = await asyncio.to_thread(
                remote.prepare_launch, f"raven-node@{target.id}", target.id, command, remote_cwd="~", env={}
            )
        except remote.RemoteMachineError as exc:
            raise NodeError(str(exc)) from None
        try:
            client = await AcpClient.launch(
                name=f"raven-node@{target.id}",
                command=launched.command,
                cwd=launched.cwd,
                env={},
                preamble=launched.preamble,
            )
        except AcpConnectionError as exc:
            raise NodeError(f"could not start ssh to machine {target.label}: {exc}") from None
        try:
            hello = await client.request(protocol.HELLO, {"protocol": protocol.PROTOCOL}, timeout=_HELLO_TIMEOUT_S)
        except AcpConnectionError as exc:
            await client.close()
            raise NodeError(self._ended(launched, exc, install_name)) from None
        except AcpError as exc:
            await client.close()
            reason = exc.message if isinstance(exc, AcpRemoteError) else str(exc)
            raise NodeError(
                f"the Raven node on machine {target.label} did not complete its handshake: {reason}"
            ) from None
        digest = hello.get("digest") if isinstance(hello, dict) else None
        if not isinstance(hello, dict) or hello.get("protocol") != protocol.PROTOCOL or digest != bundle.digest():
            await client.close()
            raise NodeError(
                f"the Raven node on machine {target.label} runs different code from this Raven; "
                f"update it with `{install_hint(target)}`"
            )
        return _Node(client, launched)

    def _ended(self, launched: remote.RemoteLaunch, exc: AcpConnectionError, install_name: str | None = None) -> str:
        """``exc`` in one sentence: not installed, an ssh failure, or the node's own last word."""
        target = launched.target
        if exc.returncode in _MISSING_RCS:
            installed = f"Raven node {install_name}" if install_name else "this Raven's node"
            return f"{installed} is not installed on machine {target.label}; install it with `{install_hint(target)}`"
        if (said := remote.failure(launched, exc)) is not None:
            return said
        lines = [line.strip() for line in (exc.stderr or "").splitlines() if line.strip()]
        # The login shell's two job-control lines lead stderr on every start;
        # the node's own reason, a traceback's last line, closes it.
        last = lines[-1][:200] if lines else ""
        return f"the Raven node on machine {target.label} stopped" + (f": {last}" if last else "")

    async def _drop(self, machine_id: str) -> None:
        node = self._nodes.pop(machine_id, None)
        if node is not None:
            await node.client.close()

    async def _close_idle(self, *, keep: str) -> None:
        now = time.monotonic()
        for machine_id, node in list(self._nodes.items()):
            if machine_id != keep and now - node.used > self._idle_close_s:
                await self._drop(machine_id)


_POOL: NodePool | None = None


def get_node_pool() -> NodePool:
    """The process-wide pool."""
    global _POOL
    if _POOL is None:
        _POOL = NodePool()
    return _POOL


async def close_node_pool() -> None:
    """Close every node this process started, and forget the pool."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        await pool.close_all()


__all__ = [
    "DEFAULT_NODE_DIR",
    "IDLE_CLOSE_S",
    "NodeError",
    "NodePool",
    "close_node_pool",
    "get_node_pool",
    "install_hint",
    "node_dir",
    "node_python",
]
