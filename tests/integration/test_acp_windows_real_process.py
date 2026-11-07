"""Native Windows ACP process trees end even when their host cannot run teardown."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects"),
]

_AGENT = """\
import json, os, pathlib, subprocess, sys, time
pathlib.Path(__file__).with_name('agent.started').touch()
worker = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(120)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
for line in sys.stdin:
    request = json.loads(line)
    result = {'agent': os.getpid(), 'worker': worker.pid, 'args': sys.argv[1:],
              'cwd': os.getcwd(), 'env': os.environ.get('RAVEN_JOB_TEST')}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
time.sleep(120)
"""

_HOST = """\
import asyncio, json, os, sys
from pathlib import Path
from raven.acp_client.client import AcpClient

async def main():
    directory = Path(sys.argv[1])
    client = await AcpClient.launch(
        name='job-test', command=[sys.executable, '-u', str(directory / 'agent.py'), 'a b', 'x&y'],
        cwd=str(directory), env={'RAVEN_JOB_TEST': 'inherited'},
    )
    result = await client.request('probe', {}, timeout=15)
    result['launcher'] = client._proc.pid
    pending = directory / 'ready.tmp'
    pending.write_text(json.dumps(result), encoding='utf-8')
    pending.replace(directory / 'ready.json')
    action = await asyncio.to_thread(sys.stdin.readline)
    if action.strip() == 'exit':
        os._exit(23)
    await client.close()
    await client.close()
    (directory / 'closed').touch()
    await asyncio.Event().wait()

asyncio.run(main())
"""


def _wait_file(path: Path, host: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert host.poll() is None, log.read_text(encoding="utf-8", errors="replace")
        assert time.monotonic() < deadline, log.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.02)


@pytest.mark.parametrize(
    "end", ["kill", "exit", "close", "agent_exit_then_kill", "agent_exit_then_close", "nested_kill"]
)
def test_the_host_owns_the_entire_agent_tree(tmp_path: Path, end: str) -> None:
    import _winapi

    (tmp_path / "agent.py").write_text(_AGENT, encoding="utf-8")
    log = tmp_path / "host.log"
    handles = {}
    host_code = _HOST
    if end == "nested_kill":
        host_code = _HOST.replace(
            "asyncio.run(main())",
            "from raven.acp_client.windows import JobObject\n"
            "outer = JobObject()\nouter.assign(os.getpid())\nasyncio.run(main())",
        )
    with ExitStack() as stack:
        host = subprocess.Popen(
            [sys.executable, "-u", "-c", host_code, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stack.enter_context(log.open("wb")),
            env={**os.environ, "RAVEN_HOME": str(tmp_path / "home")},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            _wait_file(tmp_path / "ready.json", host, log)
            state = json.loads((tmp_path / "ready.json").read_text(encoding="utf-8"))
            assert state["args"] == ["a b", "x&y"]
            assert state["cwd"] == str(tmp_path)
            assert state["env"] == "inherited"
            for name in ("launcher", "agent", "worker"):
                handle = _winapi.OpenProcess(0x00100001, False, state[name])
                stack.callback(_winapi.CloseHandle, handle)
                handles[name] = handle
                assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
            if end.startswith("agent_exit"):
                _winapi.TerminateProcess(handles["agent"], 37)
                assert _winapi.WaitForSingleObject(handles["agent"], 5000) == _winapi.WAIT_OBJECT_0
                assert _winapi.WaitForSingleObject(handles["launcher"], 5000) == _winapi.WAIT_OBJECT_0
                assert _winapi.WaitForSingleObject(handles["worker"], 0) == _winapi.WAIT_TIMEOUT
            if end.endswith("kill"):
                host.kill()
                host.wait(timeout=10)
            else:
                assert host.stdin is not None
                host.stdin.write(b"exit\n" if end == "exit" else b"close\n")
                host.stdin.flush()
                if end == "exit":
                    assert host.wait(timeout=10) == 23
                else:
                    _wait_file(tmp_path / "closed", host, log)
                    assert host.poll() is None
            for name, handle in handles.items():
                assert _winapi.WaitForSingleObject(handle, 5000) == _winapi.WAIT_OBJECT_0, f"{name} survived {end}"
        finally:
            if host.poll() is None:
                host.kill()
            host.wait(timeout=10)
            if host.stdin is not None:
                host.stdin.close()
            for handle in handles.values():
                if _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT:
                    try:
                        _winapi.TerminateProcess(handle, 99)
                    except PermissionError:
                        pass
                    _winapi.WaitForSingleObject(handle, 5000)


@pytest.mark.parametrize("assigned", [False, True])
def test_host_death_during_job_setup_never_starts_an_agent(tmp_path: Path, assigned: bool) -> None:
    import _winapi

    (tmp_path / "agent.py").write_text(_AGENT, encoding="utf-8")
    hook = f"""\
from raven.acp_client.windows import JobObject
import time
assign = JobObject.assign
def pause(job, pid):
    if {assigned!r}:
        assign(job, pid)
    pending = Path(sys.argv[1]) / 'setup.tmp'
    pending.write_text(str(pid), encoding='utf-8')
    pending.replace(pending.with_name('setup.pid'))
    time.sleep(120)
JobObject.assign = pause
asyncio.run(main())
"""
    log = tmp_path / "host.log"
    handle = None
    with log.open("wb") as stderr:
        host = subprocess.Popen(
            [sys.executable, "-u", "-c", _HOST.replace("asyncio.run(main())", hook), str(tmp_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            env={**os.environ, "RAVEN_HOME": str(tmp_path / "home")},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            _wait_file(tmp_path / "setup.pid", host, log)
            pid = int((tmp_path / "setup.pid").read_text(encoding="utf-8"))
            handle = _winapi.OpenProcess(0x00100001, False, pid)
            host.kill()
            host.wait(timeout=10)
            assert _winapi.WaitForSingleObject(handle, 10000) == _winapi.WAIT_OBJECT_0
            assert not (tmp_path / "agent.started").exists()
        finally:
            if host.poll() is None:
                host.kill()
            host.wait(timeout=10)
            if handle is not None:
                try:
                    if _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT:
                        _winapi.TerminateProcess(handle, 99)
                        _winapi.WaitForSingleObject(handle, 5000)
                finally:
                    _winapi.CloseHandle(handle)


@pytest.mark.parametrize("batch", [False, True])
async def test_closing_one_connection_keeps_another_connection_running(tmp_path: Path, batch: bool) -> None:
    import _winapi

    from raven.acp_client.client import AcpClient

    script = tmp_path / "agent.py"
    script.write_text(_AGENT, encoding="utf-8")
    command = [sys.executable, "-u", str(script)]
    if batch:
        shim = tmp_path / "agent shim.cmd"
        shim.write_text(f'@"{sys.executable}" -u "{script}" %*\n', encoding="utf-8")
        command = [str(shim)]
    clients = []
    handles = []
    with ExitStack() as stack:
        try:
            for name in ("first", "second"):
                client = await AcpClient.launch(name=name, command=[*command, "argument with spaces"])
                clients.append(client)
                result = await client.request("probe", {}, timeout=15)
                assert result["args"] == ["argument with spaces"]
                for key in ("agent", "worker"):
                    handle = _winapi.OpenProcess(0x00100001, False, result[key])
                    stack.callback(_winapi.CloseHandle, handle)
                    handles.append(handle)
            await clients[0].close()
            for handle in handles[:2]:
                assert _winapi.WaitForSingleObject(handle, 5000) == _winapi.WAIT_OBJECT_0
            assert (await clients[1].request("probe", {}, timeout=15))["args"] == ["argument with spaces"]
            for handle in handles[2:]:
                assert _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            for client in clients:
                await client.close()
            for handle in handles:
                if _winapi.WaitForSingleObject(handle, 5000) == _winapi.WAIT_TIMEOUT:
                    _winapi.TerminateProcess(handle, 99)
                    _winapi.WaitForSingleObject(handle, 5000)


async def test_the_launcher_does_not_hold_an_agents_closed_stdout_open() -> None:
    from raven.acp_client.client import AcpClient

    interpreter = getattr(sys, "_base_executable", None) or sys.executable
    code = "import os, time; os.close(1); time.sleep(120)"
    client = await AcpClient.launch(name="closed-stdout", command=[interpreter, "-c", code])
    try:
        await asyncio.wait_for(asyncio.shield(client._reader_task), 10)
        assert not client.alive
        assert client._proc.returncode is None
    finally:
        await client.close()
