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

from raven.config.paths import get_media_dir


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
