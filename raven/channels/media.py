"""Shared media persistence for channel adapters.

An adapter fetches bytes from its own SDK, then hands them here to write.
Centralizing the write gives every channel the same two guarantees:

- **No path traversal** — directory components in a server-supplied name are
  stripped, so a crafted ``../../x`` can't escape the media directory.
- **No silent collisions** — the saved name is prefixed with a content hash,
  so two senders' ``report.pdf`` don't clobber each other while re-sending
  identical bytes stays idempotent.

Two adapters intentionally keep their own (equally traversal-safe) naming:
Telegram streams straight to a path via ``download_to_drive`` with a
file-id-based name, and Matrix builds an event-id-prefixed name with a
mime-guessed suffix in ``content.attachment_path``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from raven.config.paths import get_media_dir

# Platform decision behind ``file_uri_to_path``'s localhost authority, kept as a
# name so the Windows branch stays executable (and testable) on POSIX hosts.
_IS_WINDOWS = os.name == "nt"


def safe_name(name: str | None) -> str:
    """Strip directory components from a server-supplied filename."""
    return os.path.basename(name or "") or "file"


def save_media_bytes(channel: str, data: bytes, name: str | None) -> Path:
    """Persist *data* under ``<media dir>/<content-hash>_<safe name>`` and
    return the path. Traversal-safe and collision-safe (see module docstring)."""
    digest = hashlib.sha256(data).hexdigest()[:16]
    path = get_media_dir(channel) / f"{digest}_{safe_name(name)}"
    path.write_bytes(data)
    return path


def file_uri_to_path(uri: str) -> Path | None:
    r"""The local :class:`~pathlib.Path` behind a ``file://`` URI, or ``None``.

    ``url2pathname`` is the platform-aware inverse of ``Path.as_uri()``: on
    Windows it turns ``file:///C:/...`` into ``C:\...``, where a bare
    ``Path(urlparse(uri).path)`` yields ``\C:\...``, which never exists. On
    Windows a ``localhost`` authority names a UNC share only when the
    converted path is root-relative (``\share\...``), because ``url2pathname``
    folds the host away; a drive path (``file://localhost/C:/...``) keeps its
    drive. On POSIX ``file://localhost/p`` names the same local file as
    ``file:///p``, which is also what ``urllib`` reads. Any other host is
    remote and refused, mirroring ``raven.acp.methods._file_uri_to_path``.
    """
    if not uri.startswith("file://"):
        return None
    try:
        parsed = urlparse(uri)
    except ValueError:
        # urlparse raises on a bracketed host that is not a valid IPv6 literal.
        return None
    if parsed.netloc and parsed.netloc.casefold() != "localhost":
        return None
    if not parsed.path:
        return None
    try:
        local = url2pathname(parsed.path)
    except OSError:
        # A malformed Windows spelling (``file:///C::/...``) raises inside
        # ``url2pathname``; a URL that cannot be converted names no path.
        return None
    if parsed.netloc and _IS_WINDOWS:
        if local.startswith("\\") and not local.startswith("\\\\"):
            # url2pathname folds the localhost authority away; a root-relative
            # result is the share it named, so the authority stays as the UNC host.
            return Path(f"\\\\{parsed.netloc}{local}")
        if local.startswith("/") and not local.startswith("//"):
            # When the Windows branch is exercised on POSIX without patching
            # url2pathname, url2pathname returns a POSIX-rooted path.
            return Path(f"//{parsed.netloc}{local}")
    return Path(local)
