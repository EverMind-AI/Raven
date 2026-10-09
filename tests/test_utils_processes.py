"""``raven.utils.processes`` -- reading "is this pid raven's" off its argv.

The decision layer is exercised here, not the OS readers: ``ps`` and the CIM
query both answer a real machine, and the only property the suite owns is
what the callers are handed once the read is in.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("line", [None, ""])
def test_an_unreadable_command_line_has_an_unknown_identity(monkeypatch, line) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: line)
    assert processes.raven_identity(1234) is None


@pytest.mark.parametrize(
    "line",
    [
        "python -P -m raven serve --port 18792",
        "python -m raven -- gateway --page-port 18792",
        'python -m "raven" web --supervise',
        '"C:/Program Files/Raven/raven.exe" serve --port 18792',
        "/usr/local/bin/raven -- web --supervise",
    ],
)
def test_a_resident_command_has_a_positive_identity(monkeypatch, line: str) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: line)
    assert processes.raven_identity(1234) is True


@pytest.mark.parametrize(
    "line",
    [
        "python -m raven acp",
        "python -m raven tui",
        "python -m raven --version serve",
        "python -m raven --help web",
        "python -m raven_tools serve",
        "python -m ravenx serve",
        "python -m raven.other serve",
        "python -m raven-tools serve",
        "python -m raven serve_other",
        "python -m raven serve-other",
        "python -m raven",
        "python app.py",
    ],
)
def test_a_foreign_command_has_a_negative_identity(monkeypatch, line: str) -> None:
    monkeypatch.setattr(processes, "command_line", lambda _pid: line)
    assert processes.raven_identity(1234) is False
    assert processes.looks_like_raven(1234) is False


@pytest.mark.parametrize("reader", [processes._command_line_posix, processes._command_line_windows])
def test_undecodable_argv_does_not_crash_the_reader(monkeypatch, reader) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "powershell.exe")
    run = subprocess.run

    def emit_bytes(_argv, **kwargs):
        script = r"import sys; sys.stdout.buffer.write(b'python /tmp/profil\xe9/worker.py\n')"
        return run([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "run", emit_bytes)
    assert reader(1234) == "python /tmp/profil\ufffd/worker.py"


@pytest.mark.parametrize("reader", [processes._command_line_posix, processes._command_line_windows])
@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("probe", 1)])
def test_a_failed_reader_keeps_the_identity_unknown(monkeypatch, reader, error) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "powershell.exe")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert reader(1234) is None


@pytest.mark.skipif(sys.platform != "win32", reason="CIM is the Windows reader")
@pytest.mark.slow
def test_windows_reads_unicode_arguments_from_a_live_process() -> None:
    argument = "profile-\u00fc"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", argument])
    try:
        line = processes.command_line(sleeper.pid)
        assert line is not None
        assert argument in line
        assert "time.sleep" in line
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


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
