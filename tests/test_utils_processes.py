"""``raven.utils.processes`` -- reading "is this pid raven's" off its argv.

The decision layer is exercised here, not the OS readers: ``/proc`` and the
CIM query both answer a real machine, and the only property the suite owns is
what the callers are handed once the read is in.
"""

from __future__ import annotations

from raven.utils import processes


def test_a_module_invocation_is_recognised_anywhere_in_the_argv(monkeypatch) -> None:
    """``-P`` and friends land between the interpreter and ``-m raven``."""
    monkeypatch.setattr(
        processes, "command_line", lambda _pid: "c:/py/python.exe -P -m raven gateway --page-port 18792"
    )
    assert processes.looks_like_raven(1234)


def test_another_raven_s_own_argv_is_not_ours(monkeypatch) -> None:
    """A console script is ``raven.exe ...`` with no ``-m``: the launcher that
    ends up in the record is the supervisor's ``-m`` child, and reading either
    for the other mistakes an unrelated raven for the process a state file
    names."""
    monkeypatch.setattr(processes, "command_line", lambda _pid: "c:/raven/raven.exe web --port 18792")
    assert not processes.looks_like_raven(1234)


def test_an_unreadable_command_line_is_never_ours(monkeypatch) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: None)
    assert not processes.looks_like_raven(1234)


def test_a_subdirectory_named_raven_does_not_read_as_a_module_flag(monkeypatch) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: "d:/projects/raven/python.exe app.py")
    assert not processes.looks_like_raven(1234)
