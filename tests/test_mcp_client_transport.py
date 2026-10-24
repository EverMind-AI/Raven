"""open_mcp_transport hands back streams without performing a handshake."""

import asyncio
import json
import sys

import pytest

from raven.config.schema import MCPServerConfig
from raven.mcp.client import open_mcp_transport


@pytest.mark.asyncio
async def test_stdio_transport_yields_streams_without_initialize():
    # A server that would fail an initialize but is fine to merely spawn: the
    # point is that this layer does not talk to it.
    cfg = MCPServerConfig(command=sys.executable, args=["-c", "import sys; sys.stdin.read()"])
    async with open_mcp_transport(cfg, "stdio") as (read, write):
        assert read is not None
        assert write is not None


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

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None):
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
        self.started: list[tuple[str, list[str], dict | None]] = []

    async def start_process(self, command, args, env=None):
        self.started.append((command, list(args), env))
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

    def package_launch(argv, env, cwd=None, *, local=True, deadline=None, cache_root=None):
        asked.append(local)
        return [argv[0], "--default-index", mirrors.PYPI_INDEX, *argv[1:]]

    monkeypatch.setattr(mirrors, "package_launch", package_launch)
    executor = _SpawningExecutor()

    async with open_mcp_transport(
        MCPServerConfig(command="uvx", args=["mcp-server-fetch"]), "stdio", executor=executor
    ):
        pass

    assert executor.started == [("uvx", ["--default-index", mirrors.PYPI_INDEX, "mcp-server-fetch"], None)]
    assert asked == [False]


@pytest.mark.asyncio
async def test_unknown_transport_is_the_callers_problem():
    cfg = MCPServerConfig(command="/bin/true")
    with pytest.raises(KeyError):
        async with open_mcp_transport(cfg, "carrier-pigeon"):
            pass
