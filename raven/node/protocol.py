"""The wire between a Raven and the Raven Subagent Server on a registered machine.

JSON-RPC 2.0, one object per line: the framing ACP uses, so the host drives a
machine with the client it drives an ACP agent with
(:class:`raven.acp_client.client.AcpClient`). On the machine a daemon answers
(:mod:`raven.node.server`); the host reaches it through a relay started over
ssh (``python -I -m raven.node --attach``), which bridges the ssh session to
the daemon's local socket and starts the daemon when none is up. The daemon
opens no network port.

One connection per machine carries everything the host asks of it. Agents the
daemon starts for the host each get a *channel* of that connection, on which
the agent's ACP frames travel in both directions as ``agent/frame``
notifications (a CLI agent's output lines travel the same way, every one of
them, a blank line included, since they are its answer; its prompt reaches
its stdin as ``agent/stdin``) -- and a *task* id of the host's
choosing, which outlives the connection: an agent keeps running when the ssh
goes, its frames are kept on the machine, and a new connection picks them up
from where the host stopped reading (``agent/attach``). Requests are answered
as they finish, not in the order they arrive: a search of a large tree does
not hold a heartbeat behind it.

Every ``fs/*`` call carries ``roots``, the directories it may touch. The host
decides them; the daemon checks them again, because only the machine can
resolve its own symlinks.

An MCP server the host holds for an agent is lent across the same connection.
The agent spawns the install's bridge (``python -I -m raven.node --mcp``) as a
stdio MCP server; the bridge connects to the daemon's socket and opens a
*stream* for its task (``mcp/open``, with the token the daemon left beside the
task's record at launch), and from then on the frames travel as ``mcp/frame``
notifications between the bridge and the host. The host checks the authenticated
task against the dispatch's endpoint grant before connecting to that endpoint
(:func:`pieces` says how a frame is
cut to fit a line). Nothing of a server's definition reaches the machine; the
daemon relays and keeps nothing.
"""

from __future__ import annotations

#: Bumped when a method or a field changes meaning. A daemon and a host that
#: disagree refuse each other at ``node/hello`` instead of misreading a reply.
PROTOCOL = 6

HELLO = "node/hello"
PING = "node/ping"
SHUTDOWN = "node/shutdown"

PROBE_MACHINE = "probe/machine"
PROBE_AGENT = "probe/agent"
SESSION_DIR = "session/dir"

AGENT_LAUNCH = "agent/launch"
AGENT_ATTACH = "agent/attach"
AGENT_FRAME = "agent/frame"
AGENT_STDIN = "agent/stdin"
AGENT_STDERR = "agent/stderr"
AGENT_EXIT = "agent/exit"
AGENT_CANCEL = "agent/cancel"
AGENT_CLOSE = "agent/close"
TASK_LIST = "task/list"

MCP_OPEN = "mcp/open"
"""A bridge on the machine asks for one of the host's servers for its task; the daemon tells the host that holds it."""
MCP_FRAME = "mcp/frame"
"""One piece of one MCP frame on a stream, either way: ``text``, and ``end`` on the last piece."""
MCP_CLOSE = "mcp/close"
"""A stream is over, said by either end, with a ``reason``."""

READ = "fs/read"
LIST = "fs/list"
FIND = "fs/find"
GREP = "fs/grep"
STAT = "fs/stat"
WRITE = "fs/write"
EDIT = "fs/edit"
PULL = "fs/pull"

#: Error codes of the daemon's own, beyond JSON-RPC's, for the refusals the
#: host answers with a sentence of its own rather than the daemon's message.
NOT_FOUND = -32010
"""``agent/launch``: the command is not on the machine's PATH."""
BUSY = -32011
"""``agent/launch``: :data:`MAX_AGENTS` are already running."""
CHANGED = -32012
"""``fs/write``: the file is not as ``expect`` said it was when it was read."""
UNKNOWN_TASK = -32014
"""``agent/attach``: no task of that id is running or recorded on the machine."""
STREAM_REFUSED = -32015
"""``mcp/open``: the task is not running here, the token is not its, no host holds it, or its streams are all taken."""

#: The most bytes one ``fs/pull`` returns per chunk, before base64. A line of
#: the protocol holds one chunk and little else.
CHUNK = 1024 * 1024

#: How many agents one machine runs at once; a launch past it is refused with :data:`BUSY`.
MAX_AGENTS = 8

#: How many MCP streams one task holds open at once: one per server per session its agent runs.
MAX_STREAMS = 16

#: The most characters one ``mcp/frame`` line carries; a longer MCP frame goes as several, the last marked ``end``.
MCP_CHUNK = 256 * 1024

#: The largest MCP frame a stream carries, in bytes: what the host's own bridge allows (a tool result can hold an image).
MCP_MAX_FRAME = 32 * 1024 * 1024

#: How long a finished task's record (its frames, its exit) is kept on the
#: machine for a host to come back for.
TASK_KEEP_S = 7 * 24 * 3600

#: Third-party packages the daemon cannot run without, beside Raven's own
#: code: the only ones its file tools import (measured 2026-10-10), pinned to
#: the host's versions.
REQUIREMENTS = ("loguru", "pillow")

#: Installed when the machine can get it, not required. ``ripgrep-bin`` puts the
#: same ``rg`` beside the daemon's Python that the host has beside its own, so a
#: search reads the same on both machines; without it ``grep`` runs Raven's own
#: Python search, as it does on a host with no ``rg`` (measured 2026-10-10: one
#: registered machine's package mirror did not carry it).
SEARCH_REQUIREMENT = "ripgrep-bin"

#: Where an install looks for a package its own index does not carry.
PUBLIC_INDEX = "https://pypi.org/simple"

__all__ = [
    "AGENT_ATTACH",
    "AGENT_CANCEL",
    "AGENT_CLOSE",
    "AGENT_EXIT",
    "AGENT_FRAME",
    "AGENT_LAUNCH",
    "AGENT_STDERR",
    "AGENT_STDIN",
    "BUSY",
    "CHANGED",
    "CHUNK",
    "EDIT",
    "FIND",
    "GREP",
    "HELLO",
    "LIST",
    "MAX_AGENTS",
    "NOT_FOUND",
    "PING",
    "PROBE_AGENT",
    "PROBE_MACHINE",
    "PROTOCOL",
    "PUBLIC_INDEX",
    "PULL",
    "READ",
    "REQUIREMENTS",
    "SEARCH_REQUIREMENT",
    "SESSION_DIR",
    "SHUTDOWN",
    "STAT",
    "TASK_KEEP_S",
    "TASK_LIST",
    "UNKNOWN_TASK",
    "WRITE",
]


def pieces(frame: bytes) -> list[tuple[str, bool]]:
    """One MCP frame as the ``mcp/frame`` lines that carry it: text of at most :data:`MCP_CHUNK` characters each, the last marked.

    Cut by characters rather than bytes, so every piece is a string a line can
    hold whole; the receiver joins the pieces and writes the frame with its
    line break. An empty frame is one piece, so the end is still said.
    """
    text = frame.decode("utf-8", "replace")
    out = [(text[i : i + MCP_CHUNK], False) for i in range(0, len(text), MCP_CHUNK)] or [("", False)]
    out[-1] = (out[-1][0], True)
    return out
