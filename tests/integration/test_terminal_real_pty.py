"""Real POSIX PTY ownership, environment, output, and process lifecycle."""

import asyncio
import json
import os
import sys

import pytest

from raven.contracts.terminal import TerminalError
from raven.terminal.host import TerminalHost

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PTY host")


async def test_real_child_identity_output_and_owner_close(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_TEST_LEAK", "must-not-escape")
    host = TerminalHost()
    script = (
        "import os,json,time; "
        "print(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'tty':os.isatty(0),"
        "'env':{k:v for k,v in os.environ.items() if k.startswith(('RAVEN_','ORCA_'))}}),flush=True); "
        "print('\\x1b]0;\\u2733 Ready\\x07',end='',flush=True); time.sleep(60)"
    )
    record = await host.create(f"repo::{tmp_path}", [sys.executable, "-c", script], "worker-a", "owner-a")
    try:
        for _ in range(200):
            if host.show(record.handle).status == "idle":
                break
            await asyncio.sleep(0.01)
        child = json.loads(host.tail(record.handle).strip())
        assert child["pid"] == child["pgid"]
        assert child["tty"]
        assert child["env"]["RAVEN_TERMINAL_HANDLE"] == record.handle
        assert child["env"]["RAVEN_PANE_KEY"] == record.pane_key
        assert not any(key.startswith("ORCA_") for key in child["env"])
        assert host.show(record.handle).status == "idle"
        with pytest.raises(TerminalError) as error:
            await host.close(record.handle, "other-agent")
        assert error.value.code == "forbidden"
        await host.resize(record.handle, 100, 30)
        await host.close(record.handle, "owner-a")
        assert host.show(record.handle).liveness == "exited"
        assert host.list() == []
    finally:
        await host.shutdown()


async def test_real_output_ring_is_bounded_and_exit_is_observed(tmp_path):
    host = TerminalHost()
    record = await host.create(
        f"repo::{tmp_path}", [sys.executable, "-c", "print('x' * 300000); print('line\\n' * 3000)"], "worker-a", "human"
    )
    try:
        for _ in range(400):
            if host.show(record.handle).liveness == "exited":
                break
            await asyncio.sleep(0.01)
        assert host.show(record.handle).liveness == "exited"
        assert not host.show(record.handle).connected
        assert len(host.tail(record.handle).encode()) <= 256 * 1024
        assert len(host.tail(record.handle).splitlines()) <= 2000
    finally:
        await host.shutdown()


async def test_claude_startup_dialog_cannot_receive_automated_enter(tmp_path):
    from raven.terminal.deliver import DeliveryService

    executable = tmp_path / "claude"
    executable.symlink_to(sys.executable)
    host = TerminalHost()
    record = await host.create(
        f"repo::{tmp_path}", [str(executable), "-c", "import time; print('No, exit', flush=True); time.sleep(60)"]
    )
    try:
        assert host.state(record.handle).provider == "claude"
        assert host.state(record.handle).startup_pending
        with pytest.raises(TerminalError) as error:
            await DeliveryService(host).send(record.handle, "task")
        assert error.value.code == "agent_prompt_blocked"
        assert error.value.data["reason"] == "startup_pending"
        await host.observe_output(host.state(record.handle), b"\x1b]0;Claude Code\x07")
        assert host.state(record.handle).startup_pending
        await host.observe_output(host.state(record.handle), "\x1b]0;\u2733 Ready\x07".encode())
        assert not host.state(record.handle).startup_pending
    finally:
        await host.shutdown()
