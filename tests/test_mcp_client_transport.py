"""open_mcp_transport hands back streams without performing a handshake."""

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from raven.config.schema import MCPServerConfig
from raven.mcp.client import _child_stderr, open_mcp_transport

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_transport_yields_streams_without_initialize():
    # A server that would fail an initialize but is fine to merely spawn: the
    # point is that this layer does not talk to it.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", "import sys; sys.stdin.read()"])
    async with open_mcp_transport(cfg, "stdio") as (read, write):
        assert read is not None
        assert write is not None


def test_a_redirect_with_a_descriptor_is_what_the_child_inherits(tmp_path, monkeypatch):
    """An embedded host that pointed sys.stderr at its own log keeps that log."""
    redirected = (tmp_path / "redirected.log").open("w", encoding="utf-8")
    interpreter = (tmp_path / "interpreter.log").open("w", encoding="utf-8")
    try:
        monkeypatch.setattr(sys, "stderr", redirected)
        monkeypatch.setattr(sys, "__stderr__", interpreter)
        assert _child_stderr() is redirected
    finally:
        redirected.close()
        interpreter.close()


def test_a_redirect_without_a_descriptor_falls_back_to_the_interpreter_s_stderr(tmp_path, monkeypatch):
    """A StringIO redirect cannot be inherited; the interpreter's own stderr can."""
    interpreter = (tmp_path / "interpreter.log").open("w", encoding="utf-8")
    try:
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setattr(sys, "__stderr__", interpreter)
        assert _child_stderr() is interpreter
    finally:
        interpreter.close()


def test_a_process_with_no_interpreter_stderr_keeps_its_own_redirect(monkeypatch):
    """A windowed process has no fd 2 to lend: whatever the host redirected with stays."""
    redirected = io.StringIO()
    monkeypatch.setattr(sys, "stderr", redirected)
    monkeypatch.setattr(sys, "__stderr__", None)
    assert _child_stderr() is redirected


@pytest.mark.slow
def test_a_stdio_server_starts_when_this_process_stderr_is_no_file(tmp_path):
    """Capture, an embedded host and a StringIO redirect all hand the SDK a stderr no child can inherit.

    The SDK binds its ``errlog`` default at import, so the fault shows up only
    in a process whose stderr was replaced before the transport was first
    imported -- a fresh interpreter is the only honest reproduction.
    """
    script = tmp_path / "stderr_without_a_file.py"
    script.write_text(
        "\n".join(
            [
                "import asyncio, io, sys",
                "real, sys.stderr = sys.stderr, io.StringIO()",
                "try:",
                "    from raven.config.schema import MCPServerConfig",
                "    from raven.mcp.client import open_mcp_transport",
                "    cfg = MCPServerConfig(command=sys.executable, args=['-c', 'import sys; sys.stdin.read()'])",
                "",
                "    async def main():",
                "        async with open_mcp_transport(cfg, 'stdio') as (read, write):",
                "            assert read is not None and write is not None",
                "",
                "    asyncio.run(main())",
                "finally:",
                "    sys.stderr = real",
                "print('STARTED')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The child imports this checkout, not whatever the running venv happens to
    # point at: PYTHONPATH is the only thing that decides that, not cwd.
    previous = os.environ.get("PYTHONPATH")
    env = {**os.environ}
    env["PYTHONPATH"] = str(_ROOT) if not previous else os.pathsep.join([str(_ROOT), previous])
    run = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180, env=env, check=False
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert "STARTED" in run.stdout


def _reporting_server(observed) -> list[str]:
    """Args for a server that writes the arguments it got to ``observed``,
    then waits.

    Staged and renamed so the file existing means its content is whole.
    """
    partial = observed.with_name(observed.name + ".partial")
    code = (
        "import json, os, sys\n"
        f"open({str(partial)!r}, 'w').write(json.dumps({{'args': sys.argv[1:]}}))\n"
        f"os.replace({str(partial)!r}, {str(observed)!r})\n"
        "sys.stdin.read()\n"
    )
    return ["-c", code]


async def _reported(observed) -> dict:
    for _ in range(300):
        if observed.exists():
            break
        await asyncio.sleep(0.01)
    return json.loads(observed.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_stdio_server_starts_the_way_the_mirror_fallback_says(tmp_path, monkeypatch):
    """`uvx <server>` and `npx <server>` fetch the server on first start. Asked
    with the command line and the environment the server would see, and
    started with the answer; nothing is written to the server's configuration."""
    from raven.utils import mirrors

    observed = tmp_path / "observed.json"
    asked: list[tuple[list[str], dict[str, str], bool]] = []

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None, project=True):
        asked.append((list(argv), dict(env), local))
        return [*argv, "--as-the-answer-says"]

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    args = _reporting_server(observed)
    cfg = MCPServerConfig(command=sys.executable, args=args, env={"FROM_CONFIG": "yes"})

    async with open_mcp_transport(cfg, "stdio"):
        arrived = await _reported(observed)

    assert arrived == {"args": ["--as-the-answer-says"]}
    (argv, env, local), *_ = asked
    assert argv == [sys.executable, *args] and env["FROM_CONFIG"] == "yes"
    assert local, "the server starts on this machine, where an install for it may be made"
    assert cfg.args == args and cfg.env == {"FROM_CONFIG": "yes"}, "the configuration is left as it was"


class _SpawningExecutor:
    """A sandbox that records what it was asked to start."""

    supports_process_spawning = True

    def __init__(self) -> None:
        self.started: list[tuple[str, list[str], dict | None, str | None]] = []

    async def start_process(self, command, args, env=None, cwd=None):
        self.started.append((command, list(args), env, cwd))
        return object(), object()


@pytest.mark.asyncio
async def test_a_sandboxed_stdio_server_starts_the_same_way(monkeypatch):
    """The index goes on the command line and nowhere else: a server with no
    ``env`` of its own still gets none, which the SDK reads as its own
    default environment -- the launch every server got before the mirror.
    Nothing is installed on this machine for a server the sandbox fetches in
    there."""
    from raven.utils import mirrors

    asked: list[bool] = []

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None, project=True):
        asked.append(local)
        return [argv[0], "--default-index", mirrors.PYPI_INDEX, *argv[1:]]

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    executor = _SpawningExecutor()

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"]), "stdio", executor=executor
    ):
        pass

    assert executor.started == [("uvx", ["--default-index", mirrors.PYPI_INDEX, "mcp-server-fetch"], None, None)]
    assert asked == [False]


@pytest.mark.asyncio
async def test_a_packages_working_directory_does_not_choose_the_registry(monkeypatch):
    """The server starts in its cwd, but the mirror decision runs without it.

    A package root is third-party content: a ``.npmrc`` there would otherwise
    pick the registry and the payload the launch fetches.
    """
    from raven.utils import mirrors

    asked: list[str | None] = []

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None, project=True):
        asked.append(cwd)
        return list(argv)

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    executor = _SpawningExecutor()
    package_root = "/srv/packages/demo"

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"], cwd=package_root, source="package"),
        "stdio",
        executor=executor,
    ):
        pass

    assert asked == [None]
    assert executor.started == [("uvx", ["mcp-server-fetch"], None, package_root)]


@pytest.mark.asyncio
async def test_a_host_servers_working_directory_chooses_the_registry(monkeypatch):
    """The mirror decision reads a host server's own project ``.npmrc``.

    The host configured the server, so its cwd is the operator's content, not
    third-party content: the probe runs there and sees the registry the
    server's own launch would read.
    """
    from raven.utils import mirrors

    asked: list[str | None] = []

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None, project=True):
        asked.append(cwd)
        return list(argv)

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    executor = _SpawningExecutor()
    host_cwd = "/srv/host-project"

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"], cwd=host_cwd),
        "stdio",
        executor=executor,
    ):
        pass

    assert asked == [host_cwd]
    assert executor.started == [("uvx", ["mcp-server-fetch"], None, host_cwd)]


@pytest.mark.asyncio
async def test_the_probe_of_a_server_that_is_not_the_hosts_own_reads_no_project_file(monkeypatch):
    """A host server's probe reads the project file of the operator's own
    directory. Another one's reads none at all: with no directory of its own
    to read, the question would be asked about wherever this process runs,
    and the registry would then depend on where Raven was started.
    """
    from raven.utils import mirrors

    asked: list[tuple[str | None, bool]] = []

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None, project=True):
        asked.append((cwd, project))
        return list(argv)

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    executor = _SpawningExecutor()

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"], cwd="/srv/host-project"),
        "stdio",
        executor=executor,
    ):
        pass

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"], cwd="/srv/packages/demo", source="package"),
        "stdio",
        executor=executor,
    ):
        pass

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"], cwd="/srv/handed-over", source="playbook"),
        "stdio",
        executor=executor,
    ):
        pass

    assert asked == [("/srv/host-project", True), (None, False), (None, False)]


@pytest.mark.asyncio
async def test_unknown_transport_is_the_callers_problem():
    cfg = MCPServerConfig(command="/bin/true")
    with pytest.raises(KeyError):
        async with open_mcp_transport(cfg, "carrier-pigeon"):
            pass
