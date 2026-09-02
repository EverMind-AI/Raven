"""SIGTERM stops a real gateway through the graceful chain, not the 2s sweep.

systemd and container stops deliver SIGTERM. Card N7-F5 (landed at w82)
rewired the handler from raising KeyboardInterrupt out of a signal frame --
which the stdlib runner never converts into a main-task cancel, so teardown
fell to the runner's two-second sweep -- to an in-loop ``main_task.cancel()``.
This smoke pins the process-level truth of that wire: a real ``raven gateway``
booted against a throwaway home with a dummy provider key nothing ever calls
must exit 0 on SIGTERM and print the "Shutting down..." line only the
graceful chain reaches. Every gateway listener probes forward from its
default port, so a developer's live gateway is never collided with.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_BOOT_TIMEOUT = 90.0
_SETTLE_SECONDS = 8.0
_EXIT_TIMEOUT = 60.0
_READY_MARKER = "Agent loop started"


def _raven_bin() -> Path:
    """Same derivation ``test_acp_stdio_smoke`` uses: the console script beside
    the running interpreter, so the test and a real launch resolve the same
    binary rather than whatever PATH happens to hold."""
    return Path(sys.executable).with_name("raven.exe" if sys.platform == "win32" else "raven")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_walks_the_graceful_chain(tmp_path: Path) -> None:
    binary = _raven_bin()
    if not binary.exists():
        pytest.skip(f"raven console script not installed at {binary}")

    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    config = tmp_path / "config.json"
    # A key nothing calls: the gateway refuses to boot unconfigured, and this
    # smoke never runs a turn.
    config.write_text(json.dumps({"providers": {"anthropic": {"apiKey": "sk-ant-dummy-sigterm-smoke"}}}))

    env = dict(os.environ)
    env["RAVEN_HOME"] = str(home)
    proc = subprocess.Popen(
        [
            str(binary),
            "gateway",
            "--port",
            str(_free_port()),
            "--home",
            str(home),
            "--workspace",
            str(workspace),
            "--config",
            str(config),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    lines: list[str] = []
    try:
        deadline = time.monotonic() + _BOOT_TIMEOUT
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            if _READY_MARKER in line:
                break
            if time.monotonic() > deadline:
                pytest.fail(f"gateway never reached {_READY_MARKER!r}:\n{''.join(lines[-20:])}")
        else:
            pytest.fail(f"gateway exited before {_READY_MARKER!r} (rc={proc.wait()}):\n{''.join(lines[-20:])}")

        # Let the launch settle past the subagent ACP children so the SIGTERM
        # lands on a fully running gateway, the shape a systemd stop sees.
        time.sleep(_SETTLE_SECONDS)
        proc.send_signal(signal.SIGTERM)
        rest, _ = proc.communicate(timeout=_EXIT_TIMEOUT)
        lines.append(rest)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)

    output = "".join(lines)
    assert proc.returncode == 0, f"gateway exited {proc.returncode}:\n{output[-2000:]}"
    assert "Shutting down..." in output, output[-2000:]
