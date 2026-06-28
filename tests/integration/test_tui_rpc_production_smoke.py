"""Production-path smoke (pytest integration).

Spawns the EXACT RPC server / dispatcher / socket transport that
``raven tui`` uses, drives it from a Python child process, and asserts
the 5 production-path methods (``system.hello`` + 4 umbrella methods) all
return success.

Why pytest integration (subprocess): an umbrella registration drift bug
once let umbrella methods route correctly in dispatcher unit tests but fail
in production-path wiring. Unit tests under ``tests/test_tui_rpc_*.py`` cover
handler correctness; this file guarantees the *wiring* doesn't regress
(handshake latched + every P5-aligned method returns ``result`` not ``error``).

Sibling: ``test_tui_rpc_demo_smoke.py`` (demo-runner end-to-end smoke).

Run via pytest::

    uv run pytest tests/integration/test_tui_rpc_production_smoke.py -v

Or directly as a script (legacy invocation; same logic)::

    uv run python tests/integration/test_tui_rpc_production_smoke.py
"""

from __future__ import annotations

import os
import sys

from raven.cli import tui_commands

CHILD_SRC = r"""
import json
import os
import socket
import sys

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(os.environ["RAVEN_RPC_SOCKET"])
buf = b""


def call(method, params, rid):
    global buf
    req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
    while b"\n" not in buf:
        chunk = sock.recv(8192)
        if not chunk:
            sys.exit(2)
        buf += chunk
    line, _, buf = buf.partition(b"\n")
    return json.loads(line.decode("utf-8"))


checks = [
    ("hello   ", "system.hello",   {"client_version": "0.1.0"}),
    ("/status ", "session.status", {"session_id": "s"}),
    ("/       ", "complete.slash", {"text": "/"}),
    ("/asd    ", "slash.exec",     {"command": "asd", "session_id": "s"}),
    ("/chans s", "slash.exec",     {"command": "channels status", "session_id": "s"}),
]

failures = 0
for i, (label, method, params) in enumerate(checks, 1):
    resp = call(method, params, i)
    ok = "result" in resp
    print(f"{label}: {'OK' if ok else 'FAIL'}", flush=True)
    if not ok:
        print(f"           error: {resp.get('error')}", flush=True)
        failures += 1

sock.close()
sys.exit(1 if failures else 0)
"""


def main() -> int:
    exit_code = tui_commands.run_subprocess_with_rpc(
        sys.executable,
        ["-c", CHILD_SRC],
        cwd=os.getcwd(),
        forward_signals=False,
    )
    print(f"smoke exit={exit_code}")
    return exit_code


def test_production_path_smoke() -> None:
    """Production-path smoke for the registration-drift regression.

    Fails on any non-zero exit (handshake timeout, RPC error response, or
    socket transport failure on any of the 5 production-path methods).
    """
    assert main() == 0, (
        "production-path smoke FAILED — possible umbrella registration "
        "drift regression (an umbrella method routes in unit tests but not "
        "in the production-path wiring)."
    )


if __name__ == "__main__":
    sys.exit(main())
