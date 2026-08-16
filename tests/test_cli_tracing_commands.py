"""Unit tests for ``raven/cli/tracing_commands.py`` viewer-launch helpers.

Focus: the port-reuse guard. A live port must only be reused when it is *our*
tracing viewer (answers ``/api/health`` with ``{"ok": true}``); a foreign or
stale server holding the port must be detected so the launcher can move on.

Also covers the background-viewer lifecycle: pid file written on spawn,
``raven tracing stop`` (idempotent, never kills an unverified pid), and
start-time reuse of a live instance recorded in the pid file. Real process
reaping is left to integration/manual checks — Popen is monkeypatched here.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from typer.testing import CliRunner

from raven.cli import tracing_commands as tc
from raven.cli.commands import app

runner = CliRunner()


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
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, *_a):  # silence test server logging
        pass


def _serve(port: int, health_ok: bool) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _Handler)
    srv.health_ok = health_ok  # type: ignore[attr-defined]
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


def test_find_free_port_skips_occupied():
    port = _free_port()
    srv = _serve(port, health_ok=False)  # occupy `port`
    try:
        got = tc._find_free_port(port)
        assert got is not None
        assert got != port
    finally:
        srv.shutdown()


class _FakeProc:
    pid = 4242


def test_tracing_writes_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    spawned = {}

    def fake_popen(*args, **kwargs):
        spawned["cmd"] = args[0]
        return _FakeProc()

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(tc, "_resolve_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(tc, "_viewer_health", lambda port: bool(spawned))
    monkeypatch.setattr(tc.webbrowser, "open", lambda url: None)
    port = _free_port()

    tc._open_dashboard(port)

    data = json.loads((tmp_path / "viewer.pid").read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["port"] == port


def test_tracing_stop_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))

    r = runner.invoke(app, ["tracing", "stop"])
    assert r.exit_code == 0
    assert "not running" in r.output.lower()

    r2 = runner.invoke(app, ["tracing", "stop"])
    assert r2.exit_code == 0
    assert "not running" in r2.output.lower()


def test_tracing_stop_kills_verified_viewer(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    pidfile = tmp_path / "viewer.pid"
    pidfile.write_text(json.dumps({"pid": 4242, "port": 4318}), encoding="utf-8")
    killed = []
    monkeypatch.setattr(tc, "_pid_is_viewer", lambda pid: pid == 4242)
    monkeypatch.setattr(tc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    r = runner.invoke(app, ["tracing", "stop"])

    assert r.exit_code == 0
    assert killed == [(4242, signal.SIGTERM)]
    assert not pidfile.exists()


def test_tracing_stop_never_kills_unverified_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    pidfile = tmp_path / "viewer.pid"
    pidfile.write_text(json.dumps({"pid": 4242, "port": 4318}), encoding="utf-8")
    killed = []
    monkeypatch.setattr(tc, "_pid_is_viewer", lambda pid: False)
    monkeypatch.setattr(tc.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    r = runner.invoke(app, ["tracing", "stop"])

    assert r.exit_code == 0
    assert killed == []
    assert not pidfile.exists()


def test_tracing_reuses_live_instance_from_pidfile(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path))
    (tmp_path / "viewer.pid").write_text(json.dumps({"pid": 4242, "port": 5555}), encoding="utf-8")
    monkeypatch.setattr(tc, "_pid_is_viewer", lambda pid: pid == 4242)
    monkeypatch.setattr(tc, "_viewer_health", lambda port: port == 5555)
    opened = []
    monkeypatch.setattr(tc.webbrowser, "open", lambda url: opened.append(url))

    def no_spawn(*args, **kwargs):
        raise AssertionError("must reuse the live viewer, not spawn a new one")

    monkeypatch.setattr(tc.subprocess, "Popen", no_spawn)

    tc._open_dashboard(4318)

    assert opened == ["http://127.0.0.1:5555/"]


def test_pid_is_viewer_rejects_foreign_process():
    assert tc._pid_is_viewer(os.getpid()) is False


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


def _windows_run(calls: list, tasklist_stdout: str):
    """subprocess.run stand-in for a native Windows host: ps.exe does not
    exist (FileNotFoundError), tasklist answers with the given output."""

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "ps":
            raise FileNotFoundError("ps")
        assert argv[0] == "tasklist"
        return _FakeCompleted(tasklist_stdout)

    return run


def test_pid_is_viewer_windows_detects_node_via_tasklist(monkeypatch):
    """On win32 the check must not shell out to the missing ps.exe (whose
    FileNotFoundError used to be swallowed into a blanket False): tasklist
    filters the pid and a node image means the viewer is alive."""
    calls: list = []
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        tc.subprocess, "run", _windows_run(calls, "node.exe                     4242 Console")
    )
    assert tc._pid_is_viewer(4242) is True
    assert calls == [["tasklist", "/FI", "PID eq 4242"]]


def test_pid_is_viewer_windows_no_node_match_is_false(monkeypatch):
    calls: list = []
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(
        tc.subprocess,
        "run",
        _windows_run(calls, "INFO: No tasks are running which match the specified criteria."),
    )
    assert tc._pid_is_viewer(4242) is False


def test_pid_is_viewer_windows_tasklist_missing_is_false(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")

    def raise_missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(tc.subprocess, "run", raise_missing)
    assert tc._pid_is_viewer(4242) is False


def test_pid_is_viewer_posix_keeps_ps_command_check(monkeypatch):
    calls: list = []
    monkeypatch.setattr("sys.platform", "linux")

    def run(argv, **kwargs):
        calls.append(list(argv))
        return _FakeCompleted("node /opt/raven/tracing/viewer/server.js\n")

    monkeypatch.setattr(tc.subprocess, "run", run)
    assert tc._pid_is_viewer(4242) is True
    assert calls and calls[0][0] == "ps"
