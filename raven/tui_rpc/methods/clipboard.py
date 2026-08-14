"""``clipboard.paste`` RPC handler -- pull an image off the system clipboard.

ui-tui calls this whenever a bracketed paste arrives carrying no text, which is
what a copied image looks like from a terminal's side, and again when the user
presses the paste hotkey outright.

Scope note: the response field ``attached`` stays ``False``. Attaching would
mean holding the image against the session until the next turn consumes it, and
that store is exactly what ``image.attach`` is stubbed out for -- Raven has no
per-session attachment buffer. Claiming ``attached: true`` would make the TUI
print "image attached" for an image the model never receives. So this saves the
image where the agent's file tools can read it and returns the path in
``message``; a turn that mentions the path can actually see it. When the
attachment store lands, this handler flips one field.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.config.paths import ensure_dir, get_workspace_path
from raven.utils.helpers import (
    _image_pixel_size,
    detect_image_mime,
    estimate_image_tokens,
)

if TYPE_CHECKING:
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.session import AgentLoopFactory


_NOTHING = {"attached": False, "message": "No image found in clipboard"}

# A clipboard read that blocks is a clipboard owner that is not answering; the
# TUI is waiting on this call with its composer held, so it has to end.
_READ_TIMEOUT_SECONDS = 5

# How many saved clipboard images to keep. Bounds a directory nothing else
# ever cleans up.
_KEEP = 20

_SUFFIX_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def _decode_raw(out: bytes) -> bytes | None:
    """Reader already wrote the image bytes to stdout."""
    return out


def _decode_osascript_png(out: bytes) -> bytes | None:
    """Decode ``osascript``'s rendering of clipboard PNG data.

    It prints an AppleScript literal, not bytes: ``«data PNGf89504E47...»``,
    hex after the four-character type code. Anything else -- most often
    ``the clipboard as ... failed`` on a text-only clipboard -- decodes to None
    and is treated as "no image", which is what it means.
    """
    text = out.strip().decode("ascii", errors="ignore")
    start = text.find("PNGf")
    if start < 0:
        return None
    payload = text[start + 4 :].strip().rstrip("»").strip()
    try:
        return bytes.fromhex(payload)
    except ValueError:
        return None


def _readers() -> list[tuple[list[str], Callable[[bytes], bytes | None]]]:
    """Clipboard-image readers to try, in order, for this platform.

    Paired with a decoder because not every reader emits raw bytes. Only
    commands whose binary is on PATH are returned, so a Wayland box does not
    shell out to an X11 tool that would fail slowly on a display it cannot
    reach.
    """
    if sys.platform == "darwin":
        candidates = [
            # Faster and exact when present, but Homebrew-only.
            (["pngpaste", "-"], _decode_raw),
            # Ships with macOS, so the happy path needs nothing installed.
            # Without this the stock machine has no reader at all and every
            # paste reports an empty clipboard.
            (["osascript", "-e", "the clipboard as «class PNGf»"], _decode_osascript_png),
        ]
    elif sys.platform == "win32":
        # PowerShell is the only clipboard reader guaranteed present.
        return [
            (
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "$i=Get-Clipboard -Format Image; if($i){$m=New-Object IO.MemoryStream;"
                    "$i.Save($m,[Drawing.Imaging.ImageFormat]::Png);"
                    "[Console]::OpenStandardOutput().Write($m.ToArray(),0,$m.Length)}",
                ],
                _decode_raw,
            )
        ]
    else:
        candidates = [
            (["wl-paste", "--no-newline", "--type", "image/png"], _decode_raw),
            (["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], _decode_raw),
        ]
    return [(cmd, decode) for cmd, decode in candidates if shutil.which(cmd[0])]


def _install_hint() -> str:
    """What to install when this platform has no reader on PATH.

    Kept distinct from "the clipboard holds no image": collapsing the two tells
    a user whose clipboard *does* hold an image to go looking at the clipboard.
    """
    if sys.platform == "darwin":
        return "install pngpaste (brew install pngpaste)"
    if sys.platform == "win32":
        return "powershell was not found on PATH"
    return "install wl-clipboard (Wayland) or xclip (X11)"


async def _read_clipboard_image() -> bytes | None:
    """The clipboard's image bytes, or ``None`` when it holds no image.

    An empty result and a failing reader are the same outcome here: the
    clipboard has no image this handler can use. Only an unexpected exception
    is logged, because a missing tool is a normal desktop configuration, not a
    fault worth reporting to the user mid-paste.
    """
    for cmd, decode in _readers():
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            continue
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_READ_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            continue
        except Exception:
            logger.exception("clipboard.paste: reader {} failed", cmd[0])
            continue
        if proc.returncode != 0 or not stdout:
            continue
        data = decode(stdout)
        if data and detect_image_mime(data):
            return data
    return None


def _clipboard_dir(agent_loop_factory: "AgentLoopFactory | None") -> Path:
    """Where saved clipboard images go.

    Under the live agent's workspace so the path is one the agent's file tools
    are already allowed to read -- a restricted-to-workspace run cannot open a
    temp dir elsewhere.
    """
    loop = agent_loop_factory() if agent_loop_factory is not None else None
    workspace = getattr(loop, "workspace", None) if loop is not None else None
    root = Path(workspace) if workspace else get_workspace_path()
    return ensure_dir(root / "clipboard")


def _prune(directory: Path) -> None:
    """Keep the newest ``_KEEP`` saves and drop the rest.

    Without this the directory only ever grows: every paste writes a file and
    nothing ever reads it back to delete it. A count cap rather than a TTL
    because the useful lifetime is "the turn I pasted it in", and a user who
    pastes fifty images in one session still has the recent ones.
    """
    try:
        saved = sorted(directory.glob("clipboard-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in saved[_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            # A file someone else holds open is not worth failing the paste for.
            continue


async def clipboard_paste(
    params: dict,
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> dict:
    """``clipboard.paste`` -- save the clipboard image, report where it landed."""
    if not _readers():
        # Distinct from an empty clipboard, and actionable. Saying "no image"
        # here points the user at the one thing that is not the problem.
        return {"attached": False, "message": f"No clipboard reader available: {_install_hint()}"}

    data = await _read_clipboard_image()
    if data is None:
        return dict(_NOTHING)

    mime = detect_image_mime(data) or "image/png"
    # Second resolution so two pastes inside one second cannot collide.
    name = f"clipboard-{time.strftime('%Y%m%d-%H%M%S')}-{len(data) % 10000:04d}{_SUFFIX_BY_MIME.get(mime, '.png')}"
    try:
        # Creating the directory is inside the try, not above it: it is the same
        # failure as the write (read-only mount, no space, bad permissions) and
        # left outside it would reach the client as an opaque -32603 instead of
        # the message this branch exists to produce.
        directory = _clipboard_dir(agent_loop_factory)
        path = directory / name
        path.write_bytes(data)
    except OSError as exc:
        return {"attached": False, "message": f"Could not save clipboard image: {exc}"}

    _prune(directory)

    result: dict[str, Any] = {"attached": False}
    bits = [str(path)]
    if size := _image_pixel_size(data):
        result["width"], result["height"] = size
        result["token_estimate"] = estimate_image_tokens(*size)
        bits.append(f"{size[0]}x{size[1]}")
    result["message"] = f"Saved clipboard image to {' · '.join(bits)} - mention the path to have it read"
    return result


def register_clipboard_methods(
    dispatcher: "Dispatcher",
    *,
    agent_loop_factory: "AgentLoopFactory | None" = None,
) -> None:
    """Register ``clipboard.paste`` on a dispatcher instance."""

    async def _paste(params: dict) -> dict:
        return await clipboard_paste(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("clipboard.paste", _paste)


__all__ = ["clipboard_paste", "register_clipboard_methods"]
