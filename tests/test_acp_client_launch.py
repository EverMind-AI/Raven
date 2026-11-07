"""AcpClient.launch: a command line is split the way this host reads one; an argv is taken as it is."""

from __future__ import annotations

import os
import sys

import pytest

from raven.acp_client import client as acp_client
from raven.agent.subagent.backends import env as backends_env


async def test_an_argv_is_started_as_it_is_and_no_host_grammar_reads_it_again(monkeypatch):
    monkeypatch.setattr(acp_client, "command_argv", lambda command: pytest.fail("an argv is not split again"))
    monkeypatch.setattr(backends_env, "login_shell_env", lambda: dict(os.environ))
    started: list[list[str]] = []

    def as_given(argv: list[str], child_env: dict[str, str], *, batch_files: bool = False) -> list[str]:
        started.append(list(argv))
        return argv

    monkeypatch.setattr(backends_env, "resolve_program", as_given)
    # The last word is a command for another machine's shell: one word here, whatever this host would make of it.
    argv = [sys.executable, "-c", "import sys; sys.stdin.read()", "--", "exec /bin/sh -c 'echo; node --attach'"]

    client = await acp_client.AcpClient.launch(name="argv", command=argv, env={})
    try:
        assert started == [argv]
    finally:
        await client.close()


async def test_an_empty_argv_is_refused_like_an_empty_line():
    with pytest.raises(acp_client.AcpConnectionError, match="command is empty"):
        await acp_client.AcpClient.launch(name="none", command=[], env={})
