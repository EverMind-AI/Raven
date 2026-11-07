"""raven mcp bridge: the process shell around the frame pump."""

import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.mcp.bridge import run_bridge
from raven.mcp.transport import TOKEN_BYTES, authenticate, unix_sockets_available


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
@pytest.mark.parametrize("kind", ["unix", "tcp"] if unix_sockets_available() else ["tcp"])
async def test_bridge_relays_a_frame_both_ways_and_exits_with_stdin_still_open(kind):
    with tempfile.TemporaryDirectory() as short_dir:
        sock = Path(short_dir) / "b.sock"
        token = secrets.token_hex(TOKEN_BYTES)
        seen: list[bytes] = []

        async def handle(reader, writer):
            try:
                if kind == "tcp" and not await authenticate(reader, writer, token):
                    return
                line = await reader.readline()
                seen.append(line.strip())
                writer.write(b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n')
                await writer.drain()
            finally:
                writer.close()

        if kind == "unix":
            server = await asyncio.start_unix_server(handle, path=str(sock))
            address = str(sock)
        else:
            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            address = f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}/{token}"
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "raven",
                "mcp",
                "bridge",
                address,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            proc.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
            await proc.stdin.drain()
            out = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
            assert await asyncio.wait_for(proc.wait(), timeout=10) == 0, (await proc.stderr.read()).decode()
            assert json.loads(seen[0])["method"] == "ping"
            assert json.loads(out)["result"] == {"ok": True}
        finally:
            if proc is not None:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                proc.stdin.close()
            server.close()
            await server.wait_closed()


async def test_an_authentication_failure_does_not_print_the_token(capsys):
    async def refuse(reader, writer):
        writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    token = secrets.token_hex(TOKEN_BYTES)
    address = f"tcp://127.0.0.1:{server.sockets[0].getsockname()[1]}/{token}"
    try:
        assert await run_bridge(address, sys.stdout.buffer) == 1
        message = capsys.readouterr().err
        assert "cannot reach tcp://127.0.0.1:" in message
        assert token not in message
    finally:
        server.close()
        await server.wait_closed()
