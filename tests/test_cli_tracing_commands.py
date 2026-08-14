"""Unit tests for ``raven/cli/tracing_commands.py`` viewer-launch helpers.

Focus: the port-reuse guard. A live port must only be reused when it is *our*
tracing viewer (answers ``/api/health`` with ``{"ok": true}``); a foreign or
stale server holding the port must be detected so the launcher can move on.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from raven.cli import tracing_commands as tc


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path == "/api/health" and getattr(self.server, "health_ok", False):
            body = json.dumps({"ok": True, "port": 0, "stateDir": "/x"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == tc._VIEWER_UI_PROBE and getattr(self.server, "ui_ok", False):
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            self.wfile.write(b"// app\n")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, *_a):  # silence test server logging
        pass


def _serve(port: int, health_ok: bool, ui_ok: bool = True) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    srv.health_ok = health_ok  # type: ignore[attr-defined]
    srv.ui_ok = ui_ok  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_viewer_health_false_when_nothing_listening():
    assert tc._viewer_health(_free_port()) is False


def test_viewer_health_false_for_foreign_server():
    port = _free_port()
    srv = _serve(port, health_ok=False)
    try:
        assert tc._viewer_health(port) is False
    finally:
        srv.shutdown()


def test_viewer_health_true_for_our_viewer():
    port = _free_port()
    srv = _serve(port, health_ok=True)
    try:
        assert tc._viewer_health(port) is True
    finally:
        srv.shutdown()


def test_viewer_health_false_when_ui_assets_are_gone():
    """The observed failure: a viewer whose install directory was replaced.

    Health and the HTML shell come from memory, so the process keeps reporting
    itself healthy while every asset read 404s -- reusing it hands the user an
    unstyled page that never connects. Both probes must pass, not just health.
    """
    port = _free_port()
    srv = _serve(port, health_ok=True, ui_ok=False)
    try:
        assert tc._viewer_health(port) is False
    finally:
        srv.shutdown()


def test_find_free_port_skips_occupied():
    port = _free_port()
    srv = _serve(port, health_ok=False)  # occupy `port`
    try:
        got = tc._find_free_port(port)
        assert got is not None
        assert got != port
    finally:
        srv.shutdown()
