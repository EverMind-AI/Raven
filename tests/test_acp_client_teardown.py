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

    def kill(self) -> None:
        if self.gone:
            raise ProcessLookupError
        self.killed += 1


def _as_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX branch, wherever the test runs: Windows has no SIGKILL to name, so one is lent for the read."""
    monkeypatch.setattr(acp_client, "_POSIX", True)
    monkeypatch.setattr(acp_client.signal, "SIGKILL", getattr(acp_client.signal, "SIGKILL", 9), raising=False)


def test_on_posix_the_process_group_is_killed(monkeypatch):
    swept: list[tuple[int, int]] = []
    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: swept.append((pgid, sig)), raising=False)
    proc = _Proc()

    acp_client._end(4242, proc)

    assert swept == [(4242, acp_client.signal.SIGKILL)]
    assert proc.killed == 0


def test_a_group_already_gone_is_not_an_error(monkeypatch):
    def gone(pgid, sig):
        raise ProcessLookupError

    _as_posix(monkeypatch)
    monkeypatch.setattr(os, "killpg", gone, raising=False)

    acp_client._end(4242, _Proc())


def test_without_process_groups_the_child_itself_is_killed(monkeypatch):
    monkeypatch.setattr(acp_client, "_POSIX", False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: pytest.fail("killpg has no meaning here"), raising=False)
    proc = _Proc()

    acp_client._end(4242, proc)

    assert proc.killed == 1
    acp_client._end(4242, _Proc(gone=True))


def test_the_platform_is_read_once_from_what_os_offers():
    assert acp_client._POSIX is hasattr(os, "killpg")
