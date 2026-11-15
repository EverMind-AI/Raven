"""Where a launch command's program is (raven/utils/paths.py).

A program that names a directory is on no PATH, and the two platforms look
for it in different directories: POSIX starts the child in the row's own and
execs there, while Windows' ``CreateProcess`` looks in the *caller's* own and
never in the child's. One function answers that for the launch and for the
probe that judges whether the launcher is there, so they cannot disagree.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import raven.utils.paths as paths


def _os_named(name: str) -> SimpleNamespace:
    """The real ``os``, reporting another platform's name.

    The real ``os`` is a singleton; setting ``os.name`` there would leak the
    platform swap into the whole test process. Replacing the module attribute
    leaves the true ``os`` untouched and monkeypatch restores it after.
    """
    return SimpleNamespace(**{**vars(os), "name": name})


class TestResolveDirectoryProgram:
    """The one rule for a program that names a directory."""

    def test_a_name_without_an_extension_means_exe_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CreateProcess`` runs ``agent.exe`` for a program named ``agent``."""
        monkeypatch.setattr(paths, "os", _os_named("nt"))
        expected = os.path.join(os.path.abspath("/row"), "agent.exe")
        assert paths.resolve_directory_program("./agent", "/row") == expected

    def test_posix_runs_the_file_as_it_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "os", _os_named("posix"))
        expected = os.path.join(os.path.abspath("/row"), "agent")
        assert paths.resolve_directory_program("./agent", "/row") == expected

    def test_a_name_that_carries_an_extension_keeps_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "os", _os_named("nt"))
        expected = os.path.join(os.path.abspath("/row"), "agent.cmd")
        assert paths.resolve_directory_program("./agent.cmd", "/row") == expected

    def test_an_absolute_program_is_its_own_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(paths, "os", _os_named("nt"))
        expected = os.path.join(os.path.abspath("/opt"), "agent.exe")
        assert paths.resolve_directory_program("/opt/agent", "/row") == expected

    def test_without_a_cwd_the_program_is_looked_for_where_this_process_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child starts in this process's own directory, which is where Windows looks anyway."""
        monkeypatch.setattr(paths, "os", _os_named("posix"))
        assert paths.resolve_directory_program("./agent", None) == os.path.join(os.getcwd(), "agent")
