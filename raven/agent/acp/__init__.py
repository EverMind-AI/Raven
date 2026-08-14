"""ACP (Agent Client Protocol) transport for third-party local agents.

Raven speaks ACP as the *client*: it launches an agent's ACP server as a child
process and talks JSON-RPC over its stdio. This is the second way an external
agent can be registered (``kind: "acp"`` in ``subagents.thirdParty``), beside the
older ``kind: "cli"`` which shells out once per task.

The split of concerns here:

- :mod:`raven.agent.acp.protocol` -- framing, the protocol version raven speaks,
  and the error types the layers above catch.
- :mod:`raven.agent.acp.client` -- one connection: the child process, the read
  loop, request/response correlation, and a bounded stderr tail.
- :mod:`raven.agent.acp.capabilities` -- turning one ``initialize`` handshake into
  a stored snapshot of what the agent can actually do, so the roster advertises
  measurements rather than hand-typed declarations.
"""

from raven.agent.acp.capabilities import (
    CapabilitySnapshot,
    SnapshotStore,
    default_snapshot_path,
    snapshot_fingerprint,
    verify_agent,
)
from raven.agent.acp.client import AcpClient
from raven.agent.acp.protocol import (
    PROTOCOL_VERSION,
    AcpConnectionError,
    AcpError,
    AcpProtocolError,
    AcpRemoteError,
    AcpTimeoutError,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AcpClient",
    "AcpConnectionError",
    "AcpError",
    "AcpProtocolError",
    "AcpRemoteError",
    "AcpTimeoutError",
    "CapabilitySnapshot",
    "SnapshotStore",
    "default_snapshot_path",
    "snapshot_fingerprint",
    "verify_agent",
]
