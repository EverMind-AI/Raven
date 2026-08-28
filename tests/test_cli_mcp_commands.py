"""raven mcp bridge: the process shell around the frame pump."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.mcp.bridge import run_bridge


def test_bridge_exits_nonzero_on_an_unreachable_socket(tmp_path):
    result = CliRunner().invoke(app, ["mcp", "bridge", str(tmp_path / "nope.sock")])
    assert result.exit_code == 1


@pytest.mark.asyncio
async def test_the_unreachable_socket_message_names_the_path(tmp_path, capsys):
    # Asserted here rather than through CliRunner: whether the runner separates
    # stderr from stdout changed across click majors, and the message is written
    # with print(file=sys.stderr) either way.
    sock = tmp_path / "nope.sock"
    code = await run_bridge(str(sock), sys.stdout.buffer)
    assert code == 1
    assert str(sock) in capsys.readouterr().err


@pytest.mark.asyncio
async def test_bridge_relays_a_frame_both_ways():
    # Not tmp_path: AF_UNIX caps the whole path at 104 bytes, and pytest's
    # per-test directory under a macOS TMPDIR already passes that on its own.
    with tempfile.TemporaryDirectory(dir="/tmp") as short_dir:
        sock = Path(short_dir) / "b.sock"
        seen: list[bytes] = []

        async def handle(reader, writer):
            line = await reader.readline()
            seen.append(line.strip())
            writer.write(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(sock))
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "raven",
                "mcp",
                "bridge",
                str(sock),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
            await proc.stdin.drain()
            out = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=20)
        finally:
            server.close()
            await server.wait_closed()

    assert json.loads(seen[0])["method"] == "ping"
    assert json.loads(out)["result"] == {"ok": True}
