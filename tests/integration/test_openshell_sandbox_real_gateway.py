"""OpenShellExecutor against a real local OpenShell gateway (Docker driver).

Opt in with RAVEN_OPENSHELL_IT=1 on a host whose active gateway allows bind
mounts. RAVEN_OPENSHELL_IT_IMAGE names an image with sh, python3 and curl;
RAVEN_OPENSHELL_IT_HOST names a host reachable over HTTPS for the egress case.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("openshell")
if os.environ.get("RAVEN_OPENSHELL_IT") != "1":
    pytest.skip("set RAVEN_OPENSHELL_IT=1 to run against a real OpenShell gateway", allow_module_level=True)

from raven.sandbox import openshell_executor as ox  # noqa: E402
from raven.sandbox.interfaces import SandboxInitError  # noqa: E402
from raven.sandbox.openshell_executor import OpenShellExecutor, SDKControlPlane  # noqa: E402

IMAGE = os.environ.get("RAVEN_OPENSHELL_IT_IMAGE", "osbench:1")
HOST = os.environ.get("RAVEN_OPENSHELL_IT_HOST", "mirrors.aliyun.com")
ODD_PORT_URL = os.environ.get("RAVEN_OPENSHELL_IT_ODD_PORT_URL", "http://www.portquiz.net:44232/")
UNLISTED_APEX = os.environ.get("RAVEN_OPENSHELL_IT_UNLISTED_APEX", "example.com")
SANDBOX_UID = 1000

ECHO_SERVER = """
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    sys.stderr.write("got " + req["method"] + "\\n"); sys.stderr.flush()
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"echo": req["method"]}}), flush=True)
"""


IT_ROOT = Path(os.environ.get("RAVEN_OPENSHELL_IT_ROOT", Path.home() / ".cache" / "raven-openshell-it"))
# The suite's autouse home fixture points HOME at a fresh directory per test,
# and the SDK finds the registered gateway under the XDG config home, so the
# real one is read here, at collection, before any fixture has moved HOME.
REAL_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")


@pytest.fixture(autouse=True)
def _registered_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", REAL_CONFIG_HOME)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    # The packaged gateway runs with PrivateTmp, so bind sources under /tmp
    # (where pytest's tmp_path lives) are invisible to it.
    root = IT_ROOT / tmp_path.name
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def workspace(scratch: Path) -> Path:
    ws = scratch / "ws"
    ws.mkdir()
    if os.getuid() == 0:
        os.chown(ws, SANDBOX_UID, SANDBOX_UID)
    return ws


@pytest.fixture(autouse=True)
async def _drain():
    yield
    await ox.drain_pending_deletes()


async def _gone(name: str, within: float = 30.0) -> bool:
    """Deletion is accepted at once and cleaned up asynchronously by the gateway."""
    plane = SDKControlPlane(gateway=None, workspace="default")
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if name not in [n for n, _ in plane.list_labeled(f"raven.owner={ox._owner_digest()}")]:
            return True
        await asyncio.sleep(1)
    return False


def _make(workspace: Path, **kw) -> OpenShellExecutor:
    kw.setdefault("allow_net", False)
    return OpenShellExecutor(image=IMAGE, workspace=workspace, create_timeout=120, **kw)


async def test_lifecycle_exec_and_cleanup(workspace):
    ex = _make(workspace)
    t0 = time.perf_counter()
    await ex.start()
    started = time.perf_counter() - t0
    name = ex._name
    assert name
    result = await ex.exec("echo hi > from_sandbox.txt; cat from_sandbox.txt; pwd")
    assert result.exit_code == 0, result
    assert result.stdout.split() == ["hi", "/workspace"]
    assert (workspace / "from_sandbox.txt").read_text() == "hi\n"
    (workspace / "sub").mkdir()
    assert (await ex.exec("pwd", cwd=str(workspace / "sub"))).stdout.strip() == "/workspace/sub"
    env_result = await ex.exec("echo $GREETING", env={"GREETING": "hello"})
    assert env_result.stdout.strip() == "hello"
    t1 = time.perf_counter()
    await ex.stop()
    stop_returned = time.perf_counter() - t1
    await ox.drain_pending_deletes()
    assert await _gone(name)
    assert stop_returned < 1.0
    print(f"start {started:.2f}s, stop returned in {stop_returned * 1000:.0f} ms")


async def test_timeout_returns_minus_one_quickly(workspace):
    ex = _make(workspace)
    await ex.start()
    try:
        t0 = time.perf_counter()
        result = await ex.exec("sleep 60", timeout=2)
        elapsed = time.perf_counter() - t0
        # The command's own budget, plus the short grace the gateway's report
        # needs: a local backstop that waits much longer than that is the
        # regression this bound is here for.
        assert elapsed < 2 + 3
        assert result.exit_code != 0
        print(f"timeout result exit={result.exit_code} stderr={result.stderr!r} in {elapsed:.1f}s")
    finally:
        await ex.stop()


async def test_cancel_stops_waiting(workspace):
    ex = _make(workspace)
    await ex.start()
    try:
        task = asyncio.create_task(ex.exec("sleep 60", timeout=120))
        await asyncio.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert (await ex.exec("echo still-alive")).stdout.strip() == "still-alive"
    finally:
        await ex.stop()


@pytest.mark.skipif(os.getuid() != 0, reason="needs a root host to make a workspace the sandbox user cannot write")
async def test_a_workspace_the_sandbox_user_cannot_write_is_refused(scratch):
    ws = scratch / "rootonly"
    ws.mkdir(mode=0o755)
    with pytest.raises(SandboxInitError, match="cannot write the workspace"):
        await _make(ws).start()


async def test_a_denied_request_is_reported_on_the_result(workspace):
    ex = _make(workspace, allow_net=True)
    await ex.start()
    try:
        blocked = await ex.exec(f"curl -sS -m 10 -o /dev/null https://{UNLISTED_APEX}/")
        assert blocked.exit_code != 0, blocked
        (denial,) = [d for d in blocked.denials if d.host == UNLISTED_APEX]
        assert denial.port == 443 and denial.binary.endswith("curl"), denial
        plane, name = ex._plane(), ex._name
        reads = []
        for _ in range(5):
            t0 = time.perf_counter()
            await asyncio.to_thread(plane.denial_messages, name, time.time())
            reads.append(time.perf_counter() - t0)
            assert (await ex.exec("echo ok")).denials == ()
        median = sorted(reads)[2]
        print(f"denial read: median {median * 1000:.0f} ms over {len(reads)} reads")
        assert median < 0.15
    finally:
        await ex.stop()


async def test_egress_follows_allow_net(workspace):
    closed = _make(workspace)
    opened = _make(workspace, allow_net=[HOST])
    await asyncio.gather(closed.start(), opened.start())
    try:
        probe = f"curl -sS -m 10 -o /dev/null -w '%{{http_code}}' https://{HOST}/"
        denied = await closed.exec(probe)
        allowed = await opened.exec(probe)
        other = await opened.exec("curl -sS -m 10 -o /dev/null -w '%{http_code}' https://pypi.org/")
        assert denied.exit_code != 0
        assert allowed.exit_code == 0 and allowed.stdout.strip() in {"200", "301", "302"}, allowed
        assert other.exit_code != 0
    finally:
        await asyncio.gather(closed.stop(), opened.stop())


async def test_the_open_default_reaches_any_host_and_port(workspace):
    ex = _make(workspace, allow_net=True)
    extended = _make(workspace, allow_net=["*", UNLISTED_APEX])
    t0 = time.perf_counter()
    await asyncio.gather(ex.start(), extended.start())
    started = time.perf_counter() - t0
    try:
        code = "curl -sS -m 15 -o /dev/null -w '%{http_code}' "
        apex = await ex.exec(code + "https://pypi.org/")
        multi_label = await ex.exec(code + f"http://{HOST}/")
        odd = await ex.exec(code + ODD_PORT_URL)
        other_binary = await ex.exec(
            "python3 -c \"import urllib.request;print(urllib.request.urlopen('https://pypi.org/', timeout=15).status)\""
        )
        unlisted = await ex.exec(code + f"https://{UNLISTED_APEX}/")
        unlisted_extended = await extended.exec(code + f"https://{UNLISTED_APEX}/")
        metadata = await ex.exec("curl -sS -m 5 -o /dev/null -w '%{http_code}' http://169.254.169.254/")
        assert apex.stdout.strip() == "200", apex
        assert multi_label.stdout.strip() in {"200", "301", "302"}, multi_label
        assert odd.stdout.strip() == "200", odd
        assert other_binary.stdout.strip() == "200", other_binary
        assert unlisted.exit_code != 0, unlisted
        assert unlisted_extended.stdout.strip() in {"200", "301", "302"}, unlisted_extended
        assert metadata.exit_code != 0, metadata
    finally:
        await asyncio.gather(ex.stop(), extended.stop())
    print(f"two open-network sandboxes ready in {started:.2f}s")


async def test_start_process_bridges_a_stdio_server(workspace):
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCRequest

    server = workspace / "echo_server.py"
    server.write_text(ECHO_SERVER)
    ex = _make(workspace)
    await ex.start()
    try:
        read, write = await ex.start_process("python3", ["-u", str(server)], cwd=str(workspace))
        await write.send(SessionMessage(message=JSONRPCRequest(jsonrpc="2.0", id=5, method="ping")))
        reply = await asyncio.wait_for(read.receive(), 20)
        assert reply.message.id == 5
        assert json.loads(reply.message.model_dump_json())["result"] == {"echo": "ping"}
    finally:
        await ex.stop()


async def test_sandboxes_left_by_a_dead_process_are_reaped(workspace, monkeypatch):
    plane = SDKControlPlane(gateway=None, workspace="default")
    orphan = "rvorphan" + os.urandom(3).hex()
    plane.create(
        name=orphan,
        image=IMAGE,
        policy=ox.sandbox_policy(network_policies={}, mounts=[], uid=0),
        driver_config={},
        resources={},
        labels={"raven.owner": ox._owner_digest(), "raven.pid": "999999"},
    )
    plane.wait_ready(orphan, 120)
    monkeypatch.setattr(ox, "_reaped", False)
    ex = _make(workspace)
    await ex.start()
    await ex.stop()
    await ox.drain_pending_deletes()
    assert await _gone(orphan)


async def test_eight_concurrent_sandboxes(workspace):
    executors = [_make(workspace) for _ in range(8)]
    t0 = time.perf_counter()
    await asyncio.gather(*(e.start() for e in executors))
    ready = time.perf_counter() - t0
    try:
        results = await asyncio.gather(*(e.exec("echo ok") for e in executors))
        assert all(r.stdout.strip() == "ok" for r in results)
    finally:
        await asyncio.gather(*(e.stop() for e in executors))
    print(f"8 concurrent ready in {ready:.2f}s")
