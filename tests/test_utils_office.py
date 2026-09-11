"""The owner of "convert with LibreOffice and leave nothing running", for two callers.

The viewer renders a deck to show it and the ppt engine renders one to measure
it. Both used to spawn LibreOffice themselves, with the same eleven-token argv,
and the two copies had diverged within a day: one knew how to stop a hung
conversion on Windows and the other did not. These tests pin the parts that are
shared -- the argv, the profile of its own, and a teardown that reaches the
process doing the work on either platform -- because that is what a second copy
would drift away from next.

The design engine keeps a third converter of its own, older than either and
wrapped in its own error codes; it is named in the seam test rather than quietly
covered by it, because a test that claims more than it checks is how a copy comes
back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.utils import office


def test_the_argv_asks_for_a_conversion_and_nothing_else(tmp_path: Path) -> None:
    """Eleven tokens, in this order. A conversion that asks anything -- a recovery
    prompt, a first-start wizard, a lock check -- blocks a headless run forever."""
    command = office.convert_command(
        tmp_path / "deck.pptx", tmp_path / "out", tmp_path / "profile", executable="/usr/bin/soffice"
    )

    assert command[:7] == [
        "/usr/bin/soffice",
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--norestore",
    ]
    assert command[7].startswith("-env:UserInstallation=file://"), "a profile of this run's own"
    assert str(tmp_path / "profile") in command[7]
    assert command[8:11] == ["--convert-to", "pdf", "--outdir"]
    assert command[11] == str(tmp_path / "out")
    assert command[12] == str((tmp_path / "deck.pptx").resolve())


def test_the_profile_is_this_runs_own_and_is_thrown_away(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One instance per profile: a second call against a shared one hands its work
    to the first and exits 0 having written nothing. So each run makes its own, and
    the directory it lived in does not outlive the call."""
    seen: list[str] = []

    def _fake(command, *, timeout_s):
        profile = next(token for token in command if token.startswith("-env:UserInstallation="))
        seen.append(profile)
        Path(command[command.index("--outdir") + 1], "deck.pdf").write_bytes(b"%PDF-1.4")
        return 0, "", ""

    monkeypatch.setattr(office, "_run", _fake)
    staged = tmp_path / "out"
    staged.mkdir()
    root = tmp_path / "scratch"
    root.mkdir()

    first = office.to_pdf(tmp_path / "deck.pptx", staged, executable="soffice", timeout_s=5, profile_root=root)
    second = office.to_pdf(tmp_path / "deck.pptx", staged, executable="soffice", timeout_s=5, profile_root=root)

    assert [p.name for p in first.produced] == ["deck.pdf"] and second.produced
    assert seen[0] != seen[1], "two calls, two profiles"
    assert list(root.iterdir()) == [], "and neither profile outlived its call"


class _Process:
    pid = 4321

    def __init__(self) -> None:
        self.killed = False
        self.signals: list[object] = []

    def send_signal(self, sig: object) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return None if not self.killed else 0


class _Completed:
    returncode = 0


def test_a_hung_conversion_is_torn_down_by_the_platforms_own_means(monkeypatch: pytest.MonkeyPatch) -> None:
    """soffice is a launcher, so the teardown has to reach soffice.bin behind it.

    POSIX kills the process group the spawn put it in; native Windows has neither
    os.killpg nor SIGKILL, and a hung LibreOffice left running holds the deck and
    the profile. Both paths are exercised here because CI runs one of them.
    """
    monkeypatch.setattr(office.sys, "platform", "linux")
    groups: list[tuple[int, int]] = []
    monkeypatch.setattr(office.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(office.os, "killpg", lambda pgid, sig: groups.append((pgid, sig)))
    posix = _Process()
    office.terminate(posix)
    assert groups == [(4321, office.signal.SIGKILL)], "the group, not just the launcher"
    assert not posix.killed

    def _no_group(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(office.os, "killpg", _no_group)
    gone = _Process()
    office.terminate(gone)
    assert gone.killed, "a process outside any group we can name is still killed"

    monkeypatch.setattr(office.sys, "platform", "win32")
    monkeypatch.setattr(office.signal, "CTRL_BREAK_EVENT", 6, raising=False)
    # shutil.which reads sys.platform for PATHEXT handling, and this host is not the
    # one the branch under test runs on.
    monkeypatch.setattr(office.shutil, "which", lambda name: f"C:\\Windows\\{name}.exe")
    ran: list[list[str]] = []
    monkeypatch.setattr(office.subprocess, "run", lambda argv, **kwargs: ran.append(list(argv)) or _Completed())
    windows = _Process()
    office.terminate(windows)
    assert windows.signals == [6], "the new process group is signalled"
    assert ran and ran[0][1:] == ["/F", "/T", "/PID", "4321"], "and the tree it leaves is finished"
    assert windows.killed, "nothing exited, so the last resort ran"

    # A gateway with no console attached cannot be sent CTRL_BREAK, and the tree kill
    # is the half that actually reaches soffice.bin: it must not be skipped with it.
    class _NoConsole(_Process):
        def send_signal(self, sig: object) -> None:
            raise OSError("no console to break")

    ran.clear()
    headless = _NoConsole()
    office.terminate(headless)
    assert ran and ran[0][1:] == ["/F", "/T", "/PID", "4321"], "taskkill ran without the signal"
    assert headless.killed


def test_the_viewer_and_the_ppt_engine_ask_the_same_module() -> None:
    """The seam itself, for the pair this consolidation covers.

    These two carried the identical argv and now ask this module. The design
    engine's own converter (``raven_design/rendering/office.py``) is older than
    both and is not claimed here -- asserted as a fact rather than left for a
    reader to infer a repository-wide invariant that is not yet true.
    """
    from raven.rpc import pdf_preview
    from raven_design.rendering import office as design_office
    from raven_ppt.services.render import office as engine_office

    assert pdf_preview.office is office
    assert engine_office.soffice_run is office
    assert getattr(design_office, "soffice_run", None) is None, (
        "the design engine still converts through its own backend; say so here when that changes"
    )


def test_a_windows_install_is_found_where_the_advertised_remedy_leaves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`install_hint` sends native-Windows users to `winget install
    TheDocumentFoundation.LibreOffice`. That package registers no command alias
    and puts nothing on PATH, so a PATH-only search made the advertised remedy a
    dead end: the user installs exactly what they were told to and `raven
    doctor`, the gateway preview and the render gate all still say it is
    missing. The launcher sits under the installation's `program` directory.
    """
    program_files = tmp_path / "Program Files"
    launcher = program_files / "LibreOffice" / "program" / "soffice.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("")

    monkeypatch.setattr(office.sys, "platform", "win32")
    monkeypatch.setattr(office.shutil, "which", lambda name: None)
    for variable in office._WINDOWS_PROGRAM_ROOT_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))

    assert office.find_soffice() == str(launcher)
    assert office.install_hint() == "winget install TheDocumentFoundation.LibreOffice"


def test_the_path_still_wins_and_no_install_is_still_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The install search is a fallback, not a preference: an operator who put a
    build on PATH gets that one. And a Windows host with neither answers None
    rather than a path that is not there."""
    monkeypatch.setattr(office.sys, "platform", "win32")
    for variable in office._WINDOWS_PROGRAM_ROOT_VARS:
        monkeypatch.delenv(variable, raising=False)

    monkeypatch.setattr(office.shutil, "which", lambda name: "C:\\tools\\soffice.exe" if name == "soffice" else None)
    assert office.find_soffice() == "C:\\tools\\soffice.exe"

    monkeypatch.setattr(office.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "nothing here"))
    assert office.find_soffice() is None


def test_a_posix_host_does_not_go_looking_for_a_windows_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH is the whole answer on the platforms that install through a package
    manager, so the probe does not run and cannot produce a Windows path here."""
    monkeypatch.setattr(office.sys, "platform", "linux")
    monkeypatch.setattr(office.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
    assert office.find_soffice() is None


def test_every_seat_that_reports_libreoffice_missing_asks_one_resolver() -> None:
    """The resolver, not just the conversion.

    The engine's render gate, the gateway preview and `raven doctor` each decide
    whether LibreOffice is present, and they have to decide it the same way --
    a seat that calls it missing on an install the runner would have used is the
    divergence this module exists to end. The engine re-exports rather than
    reimplements, which is the fact that keeps them equal.
    """
    from raven.cli import doctor_commands
    from raven.rpc import pdf_preview
    from raven_ppt.services.render import capabilities as engine_capabilities

    assert engine_capabilities._find_soffice is office.find_soffice
    assert pdf_preview.find_soffice() == office.find_soffice()
    assert doctor_commands._gather_external_tools().soffice == office.find_soffice()
