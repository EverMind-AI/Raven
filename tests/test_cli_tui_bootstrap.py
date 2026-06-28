"""Tests for `raven tui` Typer subcommand."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()


def test_tui_check_node_ok(monkeypatch):
    """When node is on PATH and version >= 22, --check exits 0."""
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        lambda *_args, **_kw: 0,
    )
    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 0, result.output


def test_tui_check_node_missing(monkeypatch):
    """When node not found, --check exits 1 with friendly error."""
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (None, None),
    )
    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 1
    assert "Node" in result.output
    assert "legacy-repl" in result.output


def test_tui_check_node_too_old(monkeypatch):
    """When node < 22, --check exits 1."""
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (18, 0, 0)),
    )
    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 1
    assert "22" in result.output


def test_tui_color_flag_forwards_to_child_env(monkeypatch):
    """`--color` exports RAVEN_TUI_COLOR for the Node child (read by
    colorTier.ts)."""
    import os

    monkeypatch.delenv("RAVEN_TUI_COLOR", raising=False)
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    captured: dict[str, str | None] = {}
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        lambda *_a, **_k: captured.setdefault("color", os.environ.get("RAVEN_TUI_COLOR")) or 0,
    )

    result = runner.invoke(app, ["tui", "--check", "--color", "256"])

    assert result.exit_code == 0, result.output
    assert captured["color"] == "256"


def test_tui_print_colors_uses_no_rpc_spawn(monkeypatch, tmp_path):
    """`--print-colors` is a no-RPC stdio spawn: it must use run_subprocess
    (not run_subprocess_with_rpc) and set RAVEN_TUI_PRINT_COLORS."""
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "entry.js").write_text("", encoding="utf-8")

    monkeypatch.delenv("RAVEN_TUI_PRINT_COLORS", raising=False)
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", tmp_path)
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )

    calls = {"plain": False, "rpc": False, "print_colors": None}

    def fake_plain(*_a, **_k):
        calls["plain"] = True
        calls["print_colors"] = os.environ.get("RAVEN_TUI_PRINT_COLORS")
        return 0

    def fake_rpc(*_a, **_k):
        calls["rpc"] = True
        return 3

    monkeypatch.setattr("raven.cli.tui_commands.run_subprocess", fake_plain)
    monkeypatch.setattr("raven.cli.tui_commands.run_subprocess_with_rpc", fake_rpc)

    result = runner.invoke(app, ["tui", "--print-colors"])

    assert result.exit_code == 0, result.output
    assert calls["plain"] is True
    assert calls["rpc"] is False
    assert calls["print_colors"] == "1"


def test_tui_preview_colors_uses_no_rpc_spawn(monkeypatch, tmp_path):
    """`--preview-colors` is a no-RPC stdio spawn that sets
    RAVEN_TUI_COLOR_PREVIEW."""
    import os

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "entry.js").write_text("", encoding="utf-8")

    monkeypatch.delenv("RAVEN_TUI_COLOR_PREVIEW", raising=False)
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", tmp_path)
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )

    calls = {"plain": False, "rpc": False, "preview": None}

    def fake_plain(*_a, **_k):
        calls["plain"] = True
        calls["preview"] = os.environ.get("RAVEN_TUI_COLOR_PREVIEW")
        return 0

    monkeypatch.setattr("raven.cli.tui_commands.run_subprocess", fake_plain)
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        lambda *_a, **_k: calls.__setitem__("rpc", True) or 3,
    )

    result = runner.invoke(app, ["tui", "--preview-colors"])

    assert result.exit_code == 0, result.output
    assert calls["plain"] is True
    assert calls["rpc"] is False
    assert calls["preview"] == "1"


def test_raven_node_override_no_fallback(monkeypatch, tmp_path):
    """When RAVEN_NODE is set but path missing, find_node returns (None, None)
    — must NOT fall back to venv/PATH."""
    from raven.cli.tui_commands import find_node

    monkeypatch.setenv("RAVEN_NODE", str(tmp_path / "nonexistent-node"))
    # Even if VIRTUAL_ENV / PATH would otherwise resolve a working node,
    # the explicit override must take precedence and fail closed.
    node_path, version = find_node()
    assert node_path is None, "RAVEN_NODE override must not fall back to venv/PATH"
    assert version is None


def test_dev_npx_derived_from_node_path(monkeypatch, tmp_path):
    """`--dev` mode must derive npx from the validated node_path,
    not from PATH — so RAVEN_NODE's version-pin semantics are honored."""
    # Lay out a fake node tree: /tmp.../fake-node/bin/{node,npx}
    fake_bin = tmp_path / "fake-node" / "bin"
    fake_bin.mkdir(parents=True)
    fake_node = fake_bin / "node"
    fake_npx = fake_bin / "npx"
    fake_node.write_text("#!/bin/sh\necho ok\n")
    fake_npx.write_text("#!/bin/sh\necho ok\n")

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (str(fake_node), (22, 5, 0)),
    )

    captured: dict[str, object] = {}

    # Interactive `--dev` spawns through run_subprocess_with_rpc; the npx
    # derivation logic is shared, so assert the derived binary here.
    def fake_run_subprocess_with_rpc(node_path, args, cwd, **_kw):
        captured["node_path"] = node_path
        captured["args"] = args
        captured["cwd"] = cwd
        return 0

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        fake_run_subprocess_with_rpc,
    )
    # Even if PATH has a different npx, the derived one must win.
    monkeypatch.setattr(
        "raven.cli.tui_commands.shutil.which",
        lambda _name: "/usr/bin/npx",
    )

    result = runner.invoke(app, ["tui", "--dev"])
    assert result.exit_code == 0, result.output
    assert captured["node_path"] == str(fake_npx), (
        f"Expected --dev to spawn derived npx {fake_npx}, got {captured['node_path']!r}"
    )


def test_run_subprocess_returns_child_exit_code(monkeypatch):
    """Verify run_subprocess transparently returns child's exit code."""
    from raven.cli.tui_commands import run_subprocess

    class FakeProc:
        def __init__(self, exit_code):
            self._exit_code = exit_code

        def wait(self, timeout=None):
            return self._exit_code

        def send_signal(self, sig):
            pass

    def fake_popen(*_args, **_kw):
        return FakeProc(42)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("signal.signal", lambda *_a, **_kw: None)

    result = run_subprocess("/usr/bin/node", ["dist/entry.js"], Path("/tmp"))
    assert result == 42


# ---------------------------------------------------------------------------
# RPC handshake integration
# ---------------------------------------------------------------------------


def test_rpc_handshake_timeout(monkeypatch, tmp_path):
    """When the Node child never sends `system.hello`, the parent must
    abort with exit code 3 and a diagnostic stderr message.

    We replace `find_node` with a stub so the existing Node discovery path
    is bypassed, and replace `run_subprocess_with_rpc` with a stub that
    simulates "child spawned, never sent hello → handshake_timeout()".
    """
    # Fake dist/entry.js so the build-artifact check passes
    fake_ui_dir = tmp_path / "ui-tui"
    (fake_ui_dir / "dist").mkdir(parents=True)
    (fake_ui_dir / "dist" / "entry.js").write_text("// stub")
    monkeypatch.setattr("raven.cli.tui_commands._UI_TUI_DIR", fake_ui_dir)

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )

    # The new helper must return exit_code 3 when the handshake times out.
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        lambda *_a, **_kw: 3,
    )

    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 3, result.output


def test_rpc_handshake_timeout_helper_real(tmp_path, monkeypatch):
    """End-to-end: invoke the actual `run_subprocess_with_rpc` helper with a
    child that exits immediately without ever sending `system.hello` —
    the helper must return exit code 3.

    Uses `sh -c 'sleep 6'` as a stand-in child: it inherits stdio + FDs but
    never writes anything to the request pipe → handshake timeout fires
    (we configure a 1s timeout in the test for speed).
    """
    from raven.cli import tui_commands

    # Override default handshake timeout for fast test
    monkeypatch.setattr(tui_commands, "_RPC_HANDSHAKE_TIMEOUT_S", 1.0)

    # Child that just sleeps without writing anything.
    exit_code = tui_commands.run_subprocess_with_rpc(
        "/bin/sh",
        ["-c", "sleep 6"],
        cwd=tmp_path,
        forward_signals=False,
    )
    assert exit_code == 3


# ---------------------------------------------------------------------------
# Unix-socket transport
# ---------------------------------------------------------------------------
#
# These exercise the production-path helper `_spawn_with_rpc_socket` which
# replaces `pass_fds=(3,4)` for the real `raven tui` invocation. The
# `pass_fds` variant is kept for `--check` smoke compat + existing
# Python-only tests above; do NOT remove those.


def test_rpc_socket_dir_constants_exposed():
    """Sanity: the production socket helper exposes its tempdir prefix so
    integration smoke can clean stale dirs."""
    from raven.cli import tui_commands

    assert tui_commands._RPC_SOCKET_DIR_PREFIX.startswith("eve-rpc-")


def test_production_dispatcher_includes_all_umbrella_methods():
    """Regression: prevent drift between the umbrella registrar and the
    production ``run_subprocess_with_rpc`` spawn path.

    Bug pattern: a new ``register_*_methods`` helper was added
    to the umbrella ``register_aligned_methods``, but ``tui_commands.py``
    hand-expanded the umbrella inline and forgot to add the new helper —
    so the demo runner had ``slash.exec`` / ``session.status`` /
    ``complete.{slash,path}`` registered while ``raven tui`` returned
    -32601 for the exact same methods.

    This test asserts that every method the umbrella registers is also
    registered by the production path. Future additions to the umbrella
    automatically extend this test.
    """
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods import (
        register_aligned_methods,
        register_aligned_methods_except_system,
    )
    from raven.tui_rpc.methods.system import (
        system_hello,
        system_ping,
        system_version,
    )

    umbrella = Dispatcher()
    register_aligned_methods(umbrella)
    umbrella_methods = set(umbrella.methods())

    production = Dispatcher()
    production.register("system.hello", system_hello)
    production.register("system.ping", system_ping)
    production.register("system.version", system_version)
    register_aligned_methods_except_system(production)
    production_methods = set(production.methods())

    assert umbrella_methods == production_methods, (
        f"production dispatcher drifted from umbrella; "
        f"missing in production: {umbrella_methods - production_methods}; "
        f"extra in production: {production_methods - umbrella_methods}"
    )

    # Explicit sanity: these four methods MUST be present in
    # production. These are the ones the dogfood bug surfaced.
    for required in ("slash.exec", "session.status", "complete.slash", "complete.path"):
        assert required in production_methods, (
            f"{required} missing from production dispatcher — "
            f"`raven tui` will return -32601 for it"
        )


def test_confirm_registered_when_broker_present():
    """``confirm.respond`` registers only when a ConfirmBroker is supplied
    (mirrors the emitter/turn gate). Without a broker neither the umbrella nor
    the production path registers it, so the drift test above stays balanced.
    """
    from raven.tui_rpc.confirm_broker import ConfirmBroker
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods import register_aligned_methods_except_system

    async def _send(_frame):
        return None

    broker = ConfirmBroker(_send)

    without_broker = Dispatcher()
    register_aligned_methods_except_system(without_broker)
    assert "confirm.respond" not in without_broker.methods()

    with_broker = Dispatcher()
    register_aligned_methods_except_system(with_broker, confirm_broker=broker)
    assert "confirm.respond" in with_broker.methods()


def test_rpc_socket_transport_handshake_ok(tmp_path, monkeypatch):
    """End-to-end: spawn a tiny Python child that connects to the unix socket
    via the `RAVEN_RPC_SOCKET` env var and sends ``system.hello`` — the
    parent helper must complete the handshake and the child must exit 0.

    We use a Python child rather than the real Node demo here so the test
    doesn't depend on the ui-tui build artifact; the wire protocol (newline
    JSON over the socket) is identical regardless of language.
    """
    from raven.cli import tui_commands

    # Short handshake timeout for fast feedback.
    monkeypatch.setattr(tui_commands, "_RPC_HANDSHAKE_TIMEOUT_S", 3.0)

    child_src = """
import json, os, socket, sys

sock_path = os.environ["RAVEN_RPC_SOCKET"]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock_path)
req = {"jsonrpc": "2.0", "id": 1, "method": "system.hello",
       "params": {"client_version": "0.1.0"}}
s.sendall((json.dumps(req) + "\\n").encode("utf-8"))
# Read one response frame.
buf = b""
while b"\\n" not in buf:
    chunk = s.recv(4096)
    if not chunk:
        sys.exit(2)
    buf += chunk
resp = json.loads(buf.decode("utf-8").splitlines()[0])
assert resp.get("result", {}).get("server_version"), resp
s.close()
sys.exit(0)
"""
    exit_code = tui_commands.run_subprocess_with_rpc(
        sys.executable,
        ["-c", child_src],
        cwd=tmp_path,
        forward_signals=False,
    )
    assert exit_code == 0, f"expected child to exit 0, got {exit_code}"


def test_rpc_socket_handshake_timeout_when_child_never_connects(tmp_path, monkeypatch):
    """If the spawned child never connects to the unix socket within the
    handshake deadline, parent must return exit code 3."""
    from raven.cli import tui_commands

    monkeypatch.setattr(tui_commands, "_RPC_HANDSHAKE_TIMEOUT_S", 1.0)

    # `sleep` ignores the RAVEN_RPC_SOCKET env var — pure timeout path.
    exit_code = tui_commands.run_subprocess_with_rpc(
        "/bin/sh",
        ["-c", "sleep 6"],
        cwd=tmp_path,
        forward_signals=False,
    )
    assert exit_code == 3


# ---------------------------------------------------------------------------
# Imports for the socket tests above
# ---------------------------------------------------------------------------


import sys  # noqa: E402  (kept at file bottom for the late-added tests)
