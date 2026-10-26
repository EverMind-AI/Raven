"""The gateway host's own folder dialog: the command that opens it, if there is one.

Kept apart from the ``fs.*`` handlers (raven/rpc/methods/console.py) so the
handshake (raven/rpc/methods/system.py) can announce the dialog without
importing them: those handlers reach the session methods, which import the
handshake's module, and a module reached both ways is an import cycle.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# The native folder dialog, per host. Each prints the chosen folder on stdout
# and exits non-zero (macOS) or prints nothing (the other two) when dismissed.
# Only the first tool found on PATH is offered on Linux, where no dialog ships
# with every desktop, and only with a display to put it on: neither tool starts
# without one, and exiting having printed nothing reads as a dismissal.
#
# Windows is deliberately absent. It has FolderBrowserDialog, but a gateway is
# a background process and Windows will not let one take the foreground: the
# dialog is created behind the browser and the page's click reads as nothing
# happening (measured 2026-10-22 through the gateway's own spawn). Raising it
# takes a compiled helper that forces the dialog topmost once it is up, and
# even then it cannot take the keyboard; the page walks the host's folders
# instead, which needs no foreground at all.
_PICK_DIR_PROMPT = "Choose the folder this conversation will work in"


def pick_dir_argv() -> list[str] | None:
    """The command that opens this host's folder dialog, or None where none is known."""
    if sys.platform == "darwin":
        return ["osascript", "-e", f'POSIX path of (choose folder with prompt "{_PICK_DIR_PROMPT}")']
    if sys.platform.startswith("win"):
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    for argv in (
        ["zenity", "--file-selection", "--directory", f"--title={_PICK_DIR_PROMPT}"],
        ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", _PICK_DIR_PROMPT],
    ):
        if shutil.which(argv[0]):
            return argv
    return None


def folder_dialog_available() -> bool:
    """Whether this host has a folder dialog for ``fs.pick_dir`` to open.

    What the handshake announces it on (``folder-dialog-v1``), because the page
    cannot tell for itself: a gateway in a container, or on a headless box
    reached through a forwarded port, is served at a loopback address too.
    """
    return pick_dir_argv() is not None
