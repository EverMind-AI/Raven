"""``input.detect_drop`` RPC handler -- is this pasted text a dropped file?

ui-tui asks on every submit and on every paste, because a terminal reports a
drag-and-drop as ordinary pasted text and only the shape of that text says
whether a file was meant. The client deliberately does not parse it itself
(``useSubmission.ts``: "the backend's file-drop detection handles paths with
spaces, quotes, Windows drive letters, and escaped characters correctly") -- so
the parsing lives here, once, rather than in two dialects.

Detection is existence-based, never pattern-only: something that merely looks
like a path but is not on disk is far more likely to be prose about a path, and
rewriting the user's message on that guess is worse than missing a drop.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from raven.utils.images import _image_pixel_size, detect_image_mime, estimate_image_tokens

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


# Enough for every header ``_image_pixel_size`` parses; the file is not read
# past this, so dropping a large image costs one short read.
_HEADER_BYTES = 4096

_NO_MATCH: dict = {"matched": False}


def _candidates(text: str) -> list[str]:
    """The forms one dropped path can arrive in, most-specific first.

    A terminal may hand over the path bare, shell-quoted, backslash-escaped, or
    as a ``file://`` URI, depending on the terminal and the desktop. Each is
    tried in turn rather than guessed at from the string's shape.
    """
    stripped = text.strip()
    if not stripped:
        return []

    forms = [stripped]

    if stripped.startswith("file://"):
        parsed = urlparse(stripped)
        # Any host component is a UNC share we cannot resolve locally; only the
        # empty/localhost form names a path on this machine.
        if parsed.netloc in ("", "localhost"):
            forms.append(unquote(parsed.path))

    # Quoted or backslash-escaped: shlex understands both, and returning a
    # single token is itself the evidence that the whole line was one path.
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = []
    if len(tokens) == 1:
        forms.append(tokens[0])

    # Windows drive letters survive shlex as `C:pathto` because the backslashes
    # read as escapes there, so the raw form is kept ahead of it above.
    seen: set[str] = set()
    return [f for f in forms if f and not (f in seen or seen.add(f))]


def _resolve(text: str) -> Path | None:
    """The existing file this text names, or ``None``."""
    for form in _candidates(text):
        try:
            path = Path(form).expanduser()
            if path.is_file():
                return path
        except (OSError, ValueError):
            # A form with a NUL byte or past the platform's name limit is not a
            # path; try the next reading rather than failing the whole call.
            continue
    return None


def _image_meta(path: Path) -> dict | None:
    """``{is_image, width, height, token_estimate}`` for an image, else ``None``.

    Sniffed from magic bytes rather than the suffix: a screenshot saved without
    an extension is still an image, and a ``.png`` that is really text is not.
    """
    try:
        head = path.open("rb").read(_HEADER_BYTES)
    except OSError:
        return None
    if detect_image_mime(head) is None:
        return None

    meta: dict = {"is_image": True}
    size = _image_pixel_size(head)
    if size:
        meta["width"], meta["height"] = size
        meta["token_estimate"] = estimate_image_tokens(*size)
    return meta


async def input_detect_drop(params: dict) -> dict:
    """``input.detect_drop`` -- report whether ``text`` names a file on disk.

    On a match ``text`` comes back as the resolved absolute path, which is what
    the client inserts or submits in place of the raw drop: the quoted and
    escaped forms are shell syntax, and the agent's file tools want the path.
    """
    path = _resolve(str(params.get("text") or ""))
    if path is None:
        return dict(_NO_MATCH)

    resolved = path.expanduser().resolve()
    matched: dict = {
        "matched": True,
        "name": resolved.name,
        "text": str(resolved),
        "is_image": False,
    }
    if (image := _image_meta(resolved)) is not None:
        matched.update(image)
    return matched


def register_input_methods(dispatcher: "Dispatcher") -> None:
    """Register ``input.detect_drop`` on a dispatcher instance."""
    dispatcher.register("input.detect_drop", input_detect_drop)


__all__ = ["input_detect_drop", "register_input_methods"]
