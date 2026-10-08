"""``raven.utils.processes`` -- reading "is this pid raven's" off its argv.

The decision layer is exercised here, not the OS readers: ``ps`` and the CIM
query both answer a real machine, and the only property the suite owns is
what the callers are handed once the read is in.
"""

from __future__ import annotations

from raven.utils import processes


def test_a_module_invocation_is_recognised_anywhere_in_the_argv(monkeypatch) -> None:
    """``-P`` and friends land between the interpreter and ``-m raven``."""
    monkeypatch.setattr(
        processes,
        "command_line",
        lambda _pid: "c:/py/python.exe -P -m raven gateway --page-port 18792",
    )
    assert processes.looks_like_raven(1234)


def test_a_console_script_serve_is_recognised(monkeypatch) -> None:
    """The documented entry point: ``raven serve`` runs ``commands:run`` in
    this process, so its argv is the console script plus the subcommand."""
    monkeypatch.setattr(processes, "command_line", lambda _pid: "C:/raven/Scripts/raven.exe serve --port 18792")
    assert processes.looks_like_raven(1234)

    monkeypatch.setattr(processes, "command_line", lambda _pid: "/usr/local/bin/raven web --supervise")
    assert processes.looks_like_raven(1234)

    monkeypatch.setattr(processes, "command_line", lambda _pid: "raven gateway --page-port 18792")
    assert processes.looks_like_raven(1234)


def test_a_console_script_of_another_verb_is_not(monkeypatch) -> None:
    """``raven tui`` and ``raven --version`` are run-and-done: no state file
    names them, so a pid that is one of them is not this module's business."""
    monkeypatch.setattr(processes, "command_line", lambda _pid: "C:/raven/Scripts/raven.exe tui")
    assert not processes.looks_like_raven(1234)

    monkeypatch.setattr(processes, "command_line", lambda _pid: "raven --version")
    assert not processes.looks_like_raven(1234)


def test_a_subdirectory_named_raven_does_not_read_as_a_module_flag(monkeypatch) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: "d:/projects/raven/python.exe app.py")
    assert not processes.looks_like_raven(1234)


def test_a_process_named_raven_running_something_else_is_not(monkeypatch) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: "raven app.py")
    assert not processes.looks_like_raven(1234)


def test_an_unreadable_command_line_is_never_ours(monkeypatch) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: None)
    assert not processes.looks_like_raven(1234)


class TestThePosixReader:
    """Drive the real ``ps`` against a real process, not a stub, so the
    reader the suite ships is the reader the suite measures. Skipped on
    Windows, whose reader is a different mechanism."""

    def test_a_live_process_s_argv_is_read_back(self) -> None:
        import os
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")

        line = processes.command_line(os.getpid())
        assert line is not None
        assert "pytest" in line or "python" in line

    def test_a_dead_pid_reads_as_none(self) -> None:
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")
        assert processes.command_line(99999999) is None

    def test_an_invalid_pid_reads_as_none(self) -> None:
        assert processes.command_line(0) is None
        assert processes.command_line(-1) is None

    def test_a_ps_that_fails_reads_as_none(self, monkeypatch) -> None:
        import subprocess
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")

        completed = subprocess.CompletedProcess([], 1, "", "")
        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: completed)
        assert processes._command_line_posix(1) is None

    def test_a_ps_that_cannot_run_reads_as_none(self, monkeypatch) -> None:
        """CI happens to ship ``ps``; the failure branch still has to be
        exercised, because the honest answer to "cannot read" is "do not act",
        never "assume alive"."""
        import subprocess
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")

        def _unavailable(*_a, **_k):
            raise OSError("ps: not found")

        monkeypatch.setattr(subprocess, "run", _unavailable)
        assert processes._command_line_posix(1) is None

    def test_a_subprocess_s_own_flag_is_seen(self) -> None:
        import subprocess
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")

        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            line = processes.command_line(sleeper.pid)
            assert line is not None
            assert "time.sleep" in line
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)

    def test_a_narrow_terminal_does_not_truncate_the_argv(self, monkeypatch) -> None:
        """``ps`` sizes its output from the display width it inherits, and the
        whole point of this reader is to find a flag that can sit past it."""
        import os
        import subprocess
        import sys

        if sys.platform == "win32":
            import pytest

            pytest.skip("the POSIX reader is not the Windows reader")

        monkeypatch.setenv("COLUMNS", "40")
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        sleeper = subprocess.Popen(argv, env={**os.environ, "COLUMNS": "40"})
        try:
            line = processes.command_line(sleeper.pid)
            assert line is not None
            assert argv[0] in line
            assert "time.sleep" in line
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)
