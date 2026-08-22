"""Agent Client Protocol wire framing.

The framing alone for now: newline-delimited JSON-RPC 2.0, shared by anything
that speaks ACP in either direction. The client half (a pool of agents Raven
drives as a caller) is not here yet, which is why this package holds one module
rather than the handshake, capabilities and journal that go with it.
"""

from raven.agent.acp.protocol import AcpProtocolError, decode, encode

__all__ = ["AcpProtocolError", "decode", "encode"]
