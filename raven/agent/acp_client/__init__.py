"""ACP (Agent Client Protocol), client side: Raven driving third-party local agents.

Raven speaks ACP as the *client* here -- the package name says which side, since
:mod:`raven.acp` is the other one (Raven served as an ACP agent to an editor).
It launches an agent's ACP server as a child
process and talks JSON-RPC over its stdio. This is the second way an external
agent can be registered (``kind: "acp"`` in ``subagents.thirdParty``), beside the
older ``kind: "cli"`` which shells out once per task.

The split of concerns here:

- :mod:`raven.agent.acp_client.protocol` -- framing, the protocol version raven speaks,
  and the error types the layers above catch.
- :mod:`raven.agent.acp_client.client` -- one connection: the child process, the read
  loop, request/response correlation, and a bounded stderr tail.
- :mod:`raven.agent.acp_client.capabilities` -- turning one ``initialize`` handshake into
  a stored snapshot of what the agent can actually do, so the roster advertises
  measurements rather than hand-typed declarations.
"""

from raven.agent.acp_client.capabilities import (
    CapabilitySnapshot,
    SnapshotStore,
    default_snapshot_path,
    snapshot_fingerprint,
    verify_agent,
)
from raven.agent.acp_client.client import AcpClient
from raven.agent.acp_client.protocol import (
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
