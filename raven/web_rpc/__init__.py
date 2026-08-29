"""Web-app RPC channel for the gateway.

A local WebSocket JSON-RPC endpoint a client connects to over a WebSocket.

Built for `ui-webui`, which has been retired; the C7 ruling (2026-08-29)
shrank it to what actually outlived that front end: the gateway process's
cross-process control plane. ``raven.gateway.live_probe`` reaches the live
channel adapters through this endpoint (three ``raven.channels.*`` methods),
and those adapters exist nowhere else -- without it every other surface goes
back to drawing the config file's ``enabled`` flag as if it were a connection.
The web channel streams turns exactly like the TUI (token.delta / thinking.delta
/ tool.* / message.complete), so it reuses the TUI's spine assembly and RPC
methods with ``channel="web"``; the only web-specific piece is the WebSocket
transport in :mod:`raven.web_rpc.server`.

    from raven.web_rpc.spine import build_web, WebOutlet
    from raven.web_rpc.server import WebSocketRpcServer
"""

from raven.web_rpc.server import WebSocketRpcServer
from raven.web_rpc.spine import WebOutlet, build_web

__all__ = ["WebSocketRpcServer", "WebOutlet", "build_web"]
