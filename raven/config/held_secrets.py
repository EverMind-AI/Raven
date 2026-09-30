"""The credentials Raven itself holds, so tool output can be scrubbed of them before a model reads it.

A turn that goes looking -- a shell command, a file read -- can print Raven's own
configuration, and a key printed there has entered the model's context, the
session record and the provider's logs. Measured: asked to connect an agent, a
model ran ``jq '{providers}' config.json`` and read a provider key back. The
pattern scrubbers in ``raven.security.redact`` are too eager for a coding
agent's file reads (they match placeholders in source and tests); an exact
match on the values Raven actually holds has no false positives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path
from raven.config.self_surface import is_secret_path, read_raw

#: Shorter strings are too likely to be ordinary text ("true", a port).
_MIN_LEN = 8

_cache: tuple[Path, float, tuple[tuple[str, str], ...]] | None = None


def _collect(node: Any, prefix: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for key, item in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, str):
                if len(item.strip()) >= _MIN_LEN and is_secret_path(path):
                    out.append((item.strip(), path))
            else:
                _collect(item, path, out)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _collect(item, f"{prefix}.{index}", out)


def held_secrets() -> tuple[tuple[str, str], ...]:
    """``(value, path)`` for every credential in Raven's config, longest first; re-read when the file changes."""
    global _cache
    path = get_config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ()
    if _cache is not None and _cache[0] == path and _cache[1] == mtime:
        return _cache[2]
    found: list[tuple[str, str]] = []
    try:
        _collect(read_raw(path), "", found)
    except Exception as exc:  # noqa: BLE001 - an unreadable config holds nothing to scrub
        logger.debug("held_secrets: config unreadable: {}", exc)
    unique = {value: where for value, where in found}
    held = tuple(sorted(unique.items(), key=lambda pair: -len(pair[0])))
    _cache = (path, mtime, held)
    return held


def scrub_held_secrets(text: str) -> str:
    """``text`` with every credential Raven holds replaced by where it is kept."""
    if not text:
        return text
    for value, where in held_secrets():
        if value in text:
            text = text.replace(value, f"[redacted: {where}]")
    return text


__all__ = ["held_secrets", "scrub_held_secrets"]
