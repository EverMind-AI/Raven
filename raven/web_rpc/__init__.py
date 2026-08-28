"""Web-app RPC channel for the gateway (ui-webui P1).

A local WebSocket JSON-RPC endpoint the web backend connects to as a client.
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
