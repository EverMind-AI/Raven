"""Convert a document with LibreOffice headless, and leave nothing running.

The viewer renders a deck to show it and the ppt engine
renders one to measure it, and both need the same three things: an argv that
makes LibreOffice convert without asking anything, a profile of its own for that
run, and a teardown that reaches the process doing the work. Those were written
twice, identically, and had diverged within a day of the second copy landing --
one of them knew about Windows and the other did not. What is not here is what
differs: each caller keeps its own timeout, its own idea of what counts as
failure, and its own place to put the file.

Two callers, not every caller. The design engine converts Office documents too,
through a backend of its own with its own error codes, worker directories and
configured executable; it predates this module and has not been brought through
it. Saying so is the point: the copy this module ends came back once already
because a docstring claimed something the tree did not do.

**A shared user profile silently loses a conversion.** LibreOffice allows one
instance per profile; a second invocation against the same profile hands its
request to the running instance and exits 0, having written nothing. Two decks
rendering at once is ordinary here, so every call gets a profile of its own.

**Killing the launcher is not killing the conversion.** ``soffice`` is a shell
wrapper that execs ``oosplash``, which forks ``soffice.bin``; a timeout that kills
the wrapper leaves the real process holding the profile directory open. On POSIX
the run gets a session of its own and the teardown kills the group. Native
Windows has neither ``os.killpg`` nor ``SIGKILL`` and ignores ``start_new_session``,
so the run asks for a process group there instead, the teardown signals it with
CTRL_BREAK and then finishes the tree with ``taskkill /T``. ``Popen.kill`` is the
last resort on both, because a process outside any group we can name is still one
we started.

**Exit 0 is not proof of output, and stderr is not proof of failure.** LibreOffice
reports success for documents it could not load, and a stock container prints
``failed to launch javaldx`` on every run. So this returns what appeared and what
was said, and the caller decides what that means.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

KILL_TIMEOUT_S = 10.0
"""Long enough for a tree kill to walk the tree, short enough that a wedged one
does not hold the caller past the budget it has already given up on."""


# Where a native-Windows LibreOffice puts its launcher. The MSI that `winget`
# installs registers no command alias and puts nothing on PATH, so the remedy
# `install_hint` prints is followed correctly and still leaves a PATH-only
# search finding nothing. LibreOffice documents the executable under the
# installation's `program` directory, and these are the roots an install picks
# from.
_WINDOWS_PROGRAM_ROOT_VARS = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")


RENDER_GENERATION = 3
"""Bumped whenever a change alters what a conversion on some host draws.

A cached PDF or page image is keyed by its source deck, and the deck does not
change when the fonts around it do, so a host keeps serving the boxes it drew
before a fix for as long as the cache lives. Mixed into those keys, a bump
retires every render made under the old answer. 1 is every release before
LibreOffice was given the host's Chinese fonts; 2 relied on a fontconfig file
an installer might not have written; 3 links the faces into every profile.
"""


def render_fingerprint() -> str:
    """The part of a cached render's key that stands for how it was drawn."""
    return f"g{RENDER_GENERATION}"


def find_soffice() -> str | None:
    """LibreOffice's launcher, under either of the two names it ships as.

    One resolver, because every seat that reports LibreOffice missing -- `raven
    doctor`, the gateway preview, the ppt engine's render gate -- has to agree
    with the one that runs it. A second copy of this is how the pair this module
    consolidated diverged before.
    """
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    if sys.platform == "win32":
        return _windows_install_soffice()
    if sys.platform == "darwin":
        return _macos_app_soffice()
    return None


def macos_app_dirs() -> tuple[Path, ...]:
    """Where a Mac keeps an app installed without a package manager.

    The dmg from libreoffice.org (and install.sh, where Homebrew is absent) puts
    LibreOffice.app here and nothing on PATH, so without this a Mac that has it
    is reported as having none. ``~/Applications`` is where a user who cannot
    write /Applications gets it.
    """
    return (Path("/Applications"), Path.home() / "Applications")


def _macos_app_soffice() -> str | None:
    """The launcher inside a LibreOffice.app bundle, or None."""
    for root in macos_app_dirs():
        candidate = root / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _windows_install_soffice() -> str | None:
    """The launcher inside a stock Windows installation, or None."""
    for variable in _WINDOWS_PROGRAM_ROOT_VARS:
        root = os.environ.get(variable)
        if not root:
            continue
        candidate = Path(root) / "LibreOffice" / "program" / "soffice.exe"
        if candidate.is_file():
            return str(candidate)
    return None


# The Chinese faces every Mac ships. LibreOffice 26.8's macOS build renders
# headless through a bundled fontconfig that looks for /usr/local/etc/fonts,
# which an Apple Silicon Mac does not have, and without it reaches no system or
# user font at all -- so a Chinese deck comes out as boxes. What it always reads
# is the ``user/fonts`` folder of the profile it runs with, so the faces are
# linked there. Measured 2026-09-23 (macOS 15.7, LibreOffice 26.8 from the stock
# dmg, fontconfig given an empty configuration): nothing in the profile drew no
# Han; links to these four drew PingFang, STHeiti and Songti.
MACOS_SYSTEM_HAN_FACES = (
    Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
)
# PingFang is a downloaded asset under a hashed folder that moves with macOS
# updates, so it is found each time rather than named.
MACOS_ASSET_FONTS = Path("/System/Library/AssetsV2")


def macos_han_faces() -> list[Path]:
    """The Chinese faces this Mac has today, or nothing on any other platform."""
    if sys.platform != "darwin":
        return []
    faces = [face for face in MACOS_SYSTEM_HAN_FACES if face.is_file()]
    faces += sorted(MACOS_ASSET_FONTS.glob("com_apple_MobileAsset_Font*/*/AssetData/PingFang.ttc"))[:1]
    return faces


def link_han_faces(profile: Path) -> None:
    """Link the host's Chinese faces into ``profile/user/fonts``.

    ``profile`` is the directory ``-env:UserInstallation`` names. Links rather
    than copies: the faces are the system's own, and a link whose target moved
    (PingFang after a macOS update) is replaced rather than left dangling. A
    file somebody put there by hand is left alone. Nothing here may fail a
    conversion, so an unwritable profile just renders as it did before.
    """
    faces = macos_han_faces()
    if not faces:
        return
    target = profile / "user" / "fonts"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for face in faces:
        link = target / face.name
        try:
            if link.is_symlink():
                if os.readlink(link) == str(face):
                    continue
                link.unlink()
            elif link.exists():
                continue
            link.symlink_to(face)
        except OSError:
            # A conversion started at the same moment made the same link.
            continue


def default_profile() -> Path | None:
    """The profile a plain ``soffice`` uses -- the one the model's own renders run with."""
    if sys.platform != "darwin":
        return None
    return Path.home() / "Library" / "Application Support" / "LibreOffice" / "4"


def link_han_faces_into_default_profile() -> None:
    """Give the model's own ``soffice --convert-to`` the host's Chinese faces."""
    profile = default_profile()
    if profile is not None:
        link_han_faces(profile)


def install_hint() -> str:
    """The command that installs LibreOffice on this platform, bare so a caller can phrase it.

    Every message that reports it missing ends with this. Naming the dependency
    was never the gap -- the messages already said "LibreOffice" -- but a user
    who has just been told a deck cannot be rendered still has to go and find
    out what to type, and this is not a Python package `uv` will fetch.
    """
    if sys.platform == "darwin":
        return "brew install --cask libreoffice"
    if sys.platform == "win32":
        return "winget install TheDocumentFoundation.LibreOffice"
    return "apt install libreoffice"


def convert_command(source: Path, staged: Path, profile: Path, *, executable: str, fmt: str = "pdf") -> list[str]:
    """The argv that converts `source` to `fmt` into `staged`, using `profile` alone.

    `fmt` is a LibreOffice export filter name. "pdf" is the whole document; "png"
    is the first page only, which is what a thumbnail wants.
    """
    return [
        executable,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        # A profile with nothing in it cannot offer to recover a document from a
        # previous crash, which is the other way headless startup blocks forever.
        "--norestore",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        fmt,
        "--outdir",
        str(staged),
        str(Path(source).resolve()),
    ]


@dataclass(frozen=True)
class Converted:
    """What one run left behind: the files that appeared, and what it said."""

    produced: list[Path]
    returncode: int
    stdout: str
    stderr: str


def to_pdf(
    source: Path,
    staged: Path,
    *,
    executable: str,
    timeout_s: float,
    profile_root: Path | None = None,
    fmt: str = "pdf",
) -> Converted:
    """Run one conversion of `source` into `staged` as `fmt`, and say what came of it.

    `staged` is the caller's, because where the output lands decides how it is
    moved afterwards -- a rename is not a rename across filesystems. The profile
    is this function's and is thrown away with the run; `profile_root` puts it
    somewhere other than the system temp for a caller whose temp is a tmpfs it
    would rather not fill.

    Raises plainly, and each caller maps it: `FileNotFoundError` where the
    executable will not start, `OSError` where the spawn fails otherwise, and
    `TimeoutError` where the run outlives `timeout_s` -- after the tree is down.
    """
    with tempfile.TemporaryDirectory(prefix="raven-soffice-", dir=profile_root) as scratch:
        profile = Path(scratch) / "profile"
        profile.mkdir()
        link_han_faces(profile)
        command = convert_command(Path(source), Path(staged), profile, executable=executable, fmt=fmt)
        returncode, stdout, stderr = _run(command, timeout_s=timeout_s)
    return Converted(
        produced=sorted(Path(staged).glob(f"*.{fmt}")), returncode=returncode, stdout=stdout, stderr=stderr
    )


_LIVE: set[subprocess.Popen] = set()
"""Every converter this process has started and not yet reaped.

A conversion is waited for in a thread, and cancelling the task that started
that thread does not reach either the thread or the child: the interpreter
then holds the whole process open at exit until the converter finishes on its
own. :func:`stop_running` is what a shutdown has instead."""

_LIVE_LOCK = threading.Lock()


def stop_running() -> int:
    """Stop every converter still running here, and say how many there were.

    For a shutdown, and only for one: a conversion someone is waiting on dies
    with it. The alternative is a gateway that cannot be stopped for as long as
    the longest conversion it happens to have started.
    """
    with _LIVE_LOCK:
        live = list(_LIVE)
    for process in live:
        with suppress(Exception):
            terminate(process)
    return len(live)


def _run(command: Sequence[str], *, timeout_s: float) -> tuple[int, str, str]:
    windows = sys.platform == "win32"
    process = subprocess.Popen(  # noqa: S603 - fixed argv, never a shell string
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Its own session where there are sessions, and the process group the
        # teardown can signal where there are not.
        start_new_session=not windows,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    with _LIVE_LOCK:
        _LIVE.add(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        terminate(process)
        # Reaped here so the directory the run used can be removed after it.
        process.communicate()
        raise TimeoutError(f"the conversion exceeded {timeout_s:g}s and was stopped") from exc
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(process)
    return process.returncode, stdout or "", stderr or ""


def terminate(process: subprocess.Popen) -> None:
    """Stop a converter and the children it forked, on either platform.

    Exported because every converter this repo spawns has the same problem: the
    thing that must die is not the thing that was started.
    """
    if sys.platform == "win32":
        # Two attempts, not one: CTRL_BREAK needs a console to be delivered into
        # and a service without one raises here, while the tree kill is the half
        # that actually reaches the children.
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
        try:
            taskkill = shutil.which("taskkill") or "taskkill"
            subprocess.run(  # noqa: S603, S607 - resolved above, argv is literals plus this child's pid
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=KILL_TIMEOUT_S,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        process.kill()
