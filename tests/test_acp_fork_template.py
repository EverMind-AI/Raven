"""Raven agents forked from a template that has already loaded their code (Linux only)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from raven.acp_client import fork_template
from raven.acp_client.fork_template import fork_launch, raven_agent_argv

pytestmark = pytest.mark.skipif(not fork_template.template_available(), reason="forking agents is Linux-only")


@pytest.fixture(autouse=True)
async def _no_template():
    yield
    await fork_template.close_template()


def _product(tmp_path: Path, body: str) -> Path:
    folder = tmp_path / "product"
    folder.mkdir(exist_ok=True)
    (folder / "subagent.json").write_text('{"name": "P"}', encoding="utf-8")
    script = folder / "run.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


async def _launch(script: Path, *args: str, env: dict[str, str] | None = None, cwd: str | None = None):
    base = dict(os.environ)
    endpoint = await fork_launch(
        [sys.executable, str(script), *args], {**base, **(env or {})}, cwd or str(script.parent), base_env=base
    )
    assert endpoint is not None
    return endpoint


async def _drain(endpoint) -> tuple[bytes, int]:
    out = await asyncio.wait_for(endpoint._stdout.read(), timeout=60)
    return out, await asyncio.wait_for(endpoint.wait(), timeout=60)


def test_only_a_raven_agent_on_this_interpreter_is_forked(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    loose = tmp_path / "loose.py"
    loose.write_text("pass\n")

    assert raven_agent_argv([sys.executable, str(script), "--acp"])
    assert raven_agent_argv([sys.executable, "-m", "raven", "acp"])
    assert not raven_agent_argv([sys.executable, "-m", "other"])
    assert not raven_agent_argv([sys.executable, str(loose)])
    assert not raven_agent_argv(["/usr/bin/node", str(script)])
    # Another environment's interpreter runs with its own packages, however it links.
    other = tmp_path / "venv" / "bin" / "python"
    other.parent.mkdir(parents=True)
    other.symlink_to(sys.executable)
    assert not raven_agent_argv([str(other), str(script)])


async def test_a_forked_agent_runs_as_a_fresh_interpreter_would_have(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import os, sys
        line = sys.stdin.readline().strip()
        print(line, os.getcwd(), os.environ.get("PROBE"), sys.argv[1:], sys.path[0], os.getpgid(0) == os.getpid(), flush=True)
        print("diagnostic", file=sys.stderr, flush=True)
        sys.exit(7)
        """,
    )
    endpoint = await _launch(script, "--flag", env={"PROBE": "seen"}, cwd=str(tmp_path))
    try:
        assert "forked" in endpoint.describe()
        await endpoint.write(b"hello\n")
        out, code = await _drain(endpoint)
        assert out.decode().split() == ["hello", str(tmp_path), "seen", "['--flag']", str(script.parent), "True"]
        assert await endpoint.readerr() == b"diagnostic\n"
        assert code == 7
        assert endpoint.returncode == 7
    finally:
        endpoint.release()


async def test_a_launchers_exec_of_raven_runs_raven_in_the_same_process(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import os, sys
        print(os.getpid(), flush=True)
        os.execv(sys.executable, [sys.executable, "-m", "raven", "--version"])
        raise AssertionError("unreachable")
        """,
    )
    endpoint = await _launch(script)
    try:
        out, code = await _drain(endpoint)
        pid, rest = out.decode().split("\n", 1)
        assert int(pid) == endpoint.pid
        assert "Raven v" in rest
        assert code == 0
    finally:
        endpoint.release()


async def test_ending_an_agent_kills_its_group(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print(child.pid, flush=True)
        time.sleep(600)
        """,
    )
    endpoint = await _launch(script)
    grandchild = int((await asyncio.wait_for(endpoint.readline(), timeout=60)).decode())
    await endpoint.end()
    code = await asyncio.wait_for(endpoint.wait(), timeout=30)
    endpoint.release()

    assert code == -signal.SIGKILL
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the agent's own child outlived the group kill")


async def test_an_agent_ends_with_the_template(tmp_path: Path) -> None:
    script = _product(tmp_path, "import time\nprint('up', flush=True)\ntime.sleep(600)\n")
    endpoint = await _launch(script)
    assert await asyncio.wait_for(endpoint.readline(), timeout=60) == b"up\n"
    template = fork_template._TEMPLATE
    assert template is not None and template._proc is not None

    os.kill(template._proc.pid, signal.SIGKILL)
    await asyncio.wait_for(endpoint.wait(), timeout=30)
    endpoint.release()

    for _ in range(100):
        try:
            os.kill(endpoint.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("a forked agent outlived its template")


async def test_an_agent_writes_what_it_imported_for_the_next_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fork_template, "LEARN_AFTER_S", 0.2)
    script = _product(tmp_path, "import time, colorsys\ntime.sleep(2)\n")
    endpoint = await _launch(script)
    await asyncio.wait_for(endpoint.wait(), timeout=60)
    endpoint.release()

    from raven.config.paths import get_runtime_subdir

    learned = json.loads((get_runtime_subdir("acp-template") / "modules.json").read_text())
    assert learned["python"] == list(sys.version_info[:2])
    assert list(learned["products"]) == ["product"], "learned under the folder the launcher sits in"
    assert "colorsys" in learned["products"]["product"]
    assert "__main__" not in learned["products"]["product"]


async def test_the_template_closes_once_it_has_had_no_agent_for_the_budget(tmp_path: Path, monkeypatch) -> None:
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": 0.2}}}))
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    script = _product(tmp_path, "pass\n")
    endpoint = await _launch(script)
    await asyncio.wait_for(endpoint.wait(), timeout=60)
    endpoint.release()
    template = fork_template._TEMPLATE
    assert template is not None

    for _ in range(100):
        if not template.running:
            break
        await asyncio.sleep(0.05)
    assert not template.running


async def test_anything_but_a_raven_agent_is_launched_the_ordinary_way(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("pass\n")
    base = dict(os.environ)
    assert await fork_launch([sys.executable, str(loose)], base, str(tmp_path), base_env=base) is None
    script = _product(tmp_path, "pass\n")
    assert await fork_launch([sys.executable, str(script)], base, str(tmp_path / "missing"), base_env=base) is None
    assert fork_template._TEMPLATE is None


async def test_a_template_that_cannot_start_is_not_tried_again(tmp_path: Path, monkeypatch) -> None:
    script = _product(tmp_path, "pass\n")
    started: list[int] = []

    async def refuse(self, env):
        started.append(1)
        raise RuntimeError("no room")

    monkeypatch.setattr(fork_template.ForkTemplate, "_start", refuse)
    base = dict(os.environ)
    for _ in range(2):
        assert await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base) is None
    assert started == [1]


async def test_an_acp_launch_of_a_raven_agent_is_forked_and_offered_the_relay(tmp_path: Path, monkeypatch) -> None:
    """Through the real launch: the agent runs from the template and its environment names the host's relay."""
    from raven.acp_client.client import AcpClient
    from raven.acp_client.pool import close_pool

    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", ""))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="forked", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        assert "forked" in client.endpoint.describe()
        result = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert isinstance(result, dict)
        assert seen.read_text().startswith(("unix:", "tcp:"))
    finally:
        await client.close()
        await close_pool()
    assert client.endpoint.returncode is not None


async def test_switching_both_off_launches_a_raven_agent_as_before(tmp_path: Path, monkeypatch) -> None:
    from raven.acp_client.client import AcpClient
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"subagents": {"runtime": {"llmRelay": False, "forkTemplate": False}}}))
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", "none"))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="plain", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        assert "forked" not in client.endpoint.describe()
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert seen.read_text() == "none"
    finally:
        await client.close()


def test_an_argv_the_check_cannot_read_is_not_forked(monkeypatch, tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    assert not raven_agent_argv([None, str(script)])  # type: ignore[list-item]

    def unreadable(self) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", unreadable)
    assert not raven_agent_argv([sys.executable, str(script)])


async def test_a_closed_agent_takes_no_more_input_and_its_template_counts_none(tmp_path: Path) -> None:
    script = _product(tmp_path, "import sys\nsys.stdin.read()\n")
    endpoint = await _launch(script)
    await endpoint.end()
    await asyncio.wait_for(endpoint.wait(), timeout=30)
    with pytest.raises(BrokenPipeError):
        await endpoint.write(b"late\n")
    endpoint.release()
    template = fork_template._TEMPLATE
    assert template is not None and template.children == 0


async def test_a_template_that_ended_on_its_own_is_replaced_on_the_next_launch(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None and template._proc is not None
    old = template._proc.pid
    os.kill(old, signal.SIGKILL)
    await asyncio.wait_for(template._proc.wait(), timeout=30)

    second = await _launch(script)
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    assert template._proc is not None and template._proc.pid != old


async def test_a_launch_cancels_the_pending_idle_close(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None
    template.close_when_idle(3600)
    pending = template._idle_task
    assert pending is not None
    template.close_when_idle(3600)
    assert template._idle_task is pending, "a second request does not stack a second timer"

    second = await _launch(script)
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    await asyncio.sleep(0)
    assert pending.cancelled()
    template.close_when_idle(None)
    assert template._idle_task is None or template._idle_task is not pending


async def test_a_template_that_refuses_or_cannot_be_reached_fails_the_fork_alone(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None

    with pytest.raises(RuntimeError, match="refused"):
        await template._fork("not an argv", dict(os.environ), str(tmp_path))  # type: ignore[arg-type]
    real = template._socket
    template._socket = str(tmp_path / "nobody-listens.sock")
    try:
        with pytest.raises(OSError):
            await template._fork([sys.executable, str(script)], dict(os.environ), str(tmp_path))
    finally:
        template._socket = real
    assert not template.broken


async def test_a_template_that_never_reports_ready_is_not_used(tmp_path: Path, monkeypatch) -> None:
    async def silent(proc):
        return {"ready": False, "reason": "an import started a thread (x)"}

    monkeypatch.setattr(fork_template.ForkTemplate, "_ready_report", staticmethod(silent))
    script = _product(tmp_path, "pass\n")
    base = dict(os.environ)
    assert await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base) is None
    template = fork_template._TEMPLATE
    assert template is not None and "thread" in template.broken
    with pytest.raises(RuntimeError):
        await template.spawn([sys.executable, str(script)], base, str(tmp_path), base)


async def test_the_ready_line_is_found_among_what_an_import_printed() -> None:
    from types import SimpleNamespace

    def proc(*lines: bytes):
        stream = asyncio.StreamReader()
        for line in lines:
            stream.feed_data(line)
        stream.feed_eof()
        return SimpleNamespace(stdout=stream, stderr=stream)

    report = await fork_template.ForkTemplate._ready_report(proc(b"noise\n", b'{"other": 1}\n', b'{"ready": true}\n'))
    assert report == {"ready": True}
    assert await fork_template.ForkTemplate._ready_report(proc(b"noise\n")) == {}
    assert await fork_template.ForkTemplate._ready_report(SimpleNamespace(stdout=None)) == {}
    await fork_template.ForkTemplate._log_stderr(proc(b"a warning\n"))
    await fork_template.ForkTemplate._log_stderr(SimpleNamespace(stderr=None))


async def test_a_template_built_for_other_skips_is_replaced_while_it_has_no_agent(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    base = dict(os.environ)
    first = await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base, skip=("litellm",))
    assert first is not None
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    old = fork_template._TEMPLATE

    second = await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base)
    assert second is not None
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    assert fork_template._TEMPLATE is not old and old is not None and not old.running


async def test_a_relay_that_cannot_start_leaves_the_agent_to_load_litellm(tmp_path: Path, monkeypatch) -> None:
    from raven.acp_client.client import AcpClient
    from raven.providers import relay_server

    async def no_relay():
        raise OSError("no room for a socket")

    monkeypatch.setattr(relay_server, "relay_env", no_relay)
    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", "none"))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="norelay", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert seen.read_text() == "none"
    finally:
        await client.close()


async def test_a_forked_agent_trusts_the_store_a_launched_one_would(tmp_path: Path, monkeypatch) -> None:
    """The template imports aiohttp, which builds its default TLS context on import: after the switch to the
    system's certificate store, as in an agent launched fresh."""
    monkeypatch.delenv("RAVEN_NO_SYSTEM_CA", raising=False)
    script = _product(
        tmp_path,
        "import aiohttp.connector, truststore\n"
        "print(isinstance(aiohttp.connector._SSL_CONTEXT_VERIFIED, truststore.SSLContext), flush=True)\n",
    )
    endpoint = await _launch(script)
    out, _ = await _drain(endpoint)
    endpoint.release()
    assert out == b"True\n"


# The pipes and the child watcher belong to the loop: a template still held when
# that loop closes leaves an unreaped child on a pidfd host and a transport whose
# finalizer fails on the closed loop, which pytest reports as unraisable.
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_a_template_ends_with_the_event_loop_that_started_it(tmp_path: Path) -> None:
    """A CLI turn is one asyncio.run: the template it opened is gone, reaped, when that run returns."""
    script = _product(tmp_path, "print('ran', flush=True)\n")

    async def once() -> tuple[int, bytes]:
        endpoint = await _launch(script)
        out, _ = await _drain(endpoint)
        endpoint.release()
        template = fork_template._TEMPLATE
        assert template is not None and template._proc is not None
        return template._proc.pid, out

    try:
        first_pid, first_out = asyncio.run(once())
        assert not Path(f"/proc/{first_pid}").exists(), "the template outlived its loop, or was left unreaped"
        second_pid, second_out = asyncio.run(once())
        assert first_out == second_out == b"ran\n"
        assert second_pid != first_pid
        assert not Path(f"/proc/{second_pid}").exists()
    finally:
        asyncio.run(fork_template.close_template())


async def _deaf_template(tmp_path: Path, monkeypatch) -> fork_template.ForkTemplate:
    """A template that reports ready and never reads its stdin, so letting go of it does not stop it."""
    (tmp_path / "deaf_template.py").write_text(
        "import json, time\nprint(json.dumps({'ready': True}), flush=True)\ntime.sleep(600)\n", encoding="utf-8"
    )
    monkeypatch.setattr(fork_template, "_TEMPLATE_PROGRAM", ("-m", "deaf_template"))
    template = fork_template.ForkTemplate()
    await template._start({**os.environ, "PYTHONPATH": str(tmp_path)})
    assert template.running
    return template


async def test_a_template_that_ignores_its_lifeline_is_killed_and_reaped(tmp_path: Path, monkeypatch) -> None:
    template = await _deaf_template(tmp_path, monkeypatch)
    proc = template._proc
    assert proc is not None

    await template.close()
    assert proc.returncode is not None


async def test_a_close_cut_short_still_ends_the_template_and_closes_its_pipes(tmp_path: Path, monkeypatch) -> None:
    """The loop ending cancels a close still waiting out the grace: an idle close, or the hold task's own."""
    template = await _deaf_template(tmp_path, monkeypatch)
    proc = template._proc
    assert proc is not None

    closing = asyncio.get_running_loop().create_task(template.close())
    await asyncio.sleep(0.2)
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert proc._transport.is_closing()
    await asyncio.wait_for(proc.wait(), timeout=10)


def test_the_client_shelf_loads_no_module_by_name() -> None:
    """``raven.acp_client`` sits under the surfaces, and starts the template through the entry point.

    import-linter reads import statements, so a module loaded through importlib
    or runpy is an edge no contract sees. The template's own side, which does
    load the surfaces by name, is a launcher beside the CLI.
    """
    import ast

    import raven.acp_client

    root = Path(raven.acp_client.__file__).parent
    found = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in {"import_module", "__import__", "run_module", "run_path"}:
                    found.append(f"{path.name}:{node.lineno} {name}")
    assert found == []
