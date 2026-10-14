"""AcpClient.close ends the server wherever raven runs.

On POSIX the whole process group goes, as it always did. Windows has neither
process groups ``killpg`` can reach nor SIGKILL, and a teardown that reached
for either raised before any of the cleanup after it ran -- the child stayed
alive, the pipes stayed open -- on every path that closes a client, the node
installer's handshake included.
"""

from __future__ import annotations

import os

import pytest

from raven.acp_client import client as acp_client


class _Proc:
    def __init__(self, *, gone: bool = False) -> None:
        self.gone = gone
        self.killed = 0
        self.pid = 4242
        self.returncode = None

    def kill(self) -> None:
        if self.gone:
            raise ProcessLookupError
        self.killed += 1


def _as_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX branch, wherever the test runs: Windows has no SIGKILL to name, so one is lent for the read."""
    monkeypatch.setattr(acp_client.signal, "SIGKILL", getattr(acp_client.signal, "SIGKILL", 9), raising=False)


async def test_on_posix_the_process_group_is_killed(monkeypatch):
    swept: list[tuple[int, int]] = []
    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: swept.append((pgid, sig)), raising=False)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert swept == [(4242, acp_client.signal.SIGKILL)]
    assert proc.killed == 0


async def test_a_group_already_gone_is_not_an_error(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError

    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", gone, raising=False)

    await acp_client._end(4242, _Proc())


async def test_without_process_groups_the_child_itself_is_killed(monkeypatch):
    """No ``killpg`` and no ``taskkill`` on the PATH: the child is ended by handle, and one already gone is no error."""
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(acp_client.shutil, "which", lambda name, *a, **k: None)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert proc.killed == 1
    await acp_client._end(4242, _Proc(gone=True))


async def test_without_process_groups_taskkill_walks_the_tree_first(monkeypatch):
    """Where ``taskkill`` exists it ends the launcher's children by pid, bounded, before the launcher itself."""
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.setattr(acp_client.shutil, "which", lambda name, *a, **k: "taskkill" if name == "taskkill" else None)
    calls: list[tuple[list[str], float]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs["timeout"]))
        return acp_client.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(acp_client.subprocess, "run", run)
    proc = _Proc()

    await acp_client._end(4242, proc)

    assert calls == [(["taskkill", "/T", "/F", "/PID", "4242"], acp_client._TEARDOWN_TASKKILL_S)]
    assert proc.killed == 1
    exited = _Proc()
    exited.returncode = 0
    await acp_client._end(4242, exited)
    assert len(calls) == 1, "a launcher that has exited is not walked: its pid may name another process by now"
