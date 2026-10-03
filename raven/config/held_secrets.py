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

import json
import re
from pathlib import Path
from typing import Any

from loguru import logger

from raven.config.loader import get_config_path
from raven.config.self_surface import is_secret_path, read_raw, url_credentials
from raven.security.redact import redact_home_config_read

#: Shorter strings are too likely to be ordinary text ("true", a port). Six,
#: not eight: a mailbox password (``hunter2``) is a credential too.
_MIN_LEN = 6
#: Values a credential field holds when it holds nothing.
_PLACEHOLDERS = frozenset({"EMPTY", "empty", "dummy", "changeme", "not-needed", "sk-xxx"})

_cache: tuple[Path, float, tuple[tuple[str, str], ...]] | None = None


def _collect(node: Any, prefix: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        children = [(f"{prefix}.{key}" if prefix else str(key), item) for key, item in node.items()]
    elif isinstance(node, list):
        children = [(f"{prefix}.{index}", item) for index, item in enumerate(node)]
    else:
        return
    for path, item in children:
        if isinstance(item, str):
            value = item.strip()
            if len(value) >= _MIN_LEN and value not in _PLACEHOLDERS and is_secret_path(path) and _worth_holding(path):
                out.append((value, path))
            elif "://" in value:
                out.extend((part, f"{path} (in its URL)") for part in url_credentials(value))
        else:
            _collect(item, path, out)


#: Header names that carry a credential. Every header counts as secret to the
#: gate and the card, which only decide what is shown; held values are replaced
#: in every tool result, so ``Content-Type: application/json`` must not be one.
_CREDENTIAL_HEADER = re.compile(r"(?i)(?:auth|cookie|key|token|secret|sig|session|code|pass)")
_HEADER_MAPS = frozenset({"headers", "extraheaders", "extra_headers"})


def _worth_holding(path: str) -> bool:
    parts = path.split(".")
    if len(parts) >= 2 and parts[-2].lower() in _HEADER_MAPS:
        return bool(_CREDENTIAL_HEADER.search(parts[-1]))
    return True


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
    # As a JSON file prints it too: a quote or a backslash in the value is
    # escaped there, and `cat config.json` shows that spelling.
    unique.update({json.dumps(value)[1:-1]: where for value, where in list(unique.items())})
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


def scrub_held_value(value: Any) -> Any:
    """``value`` with every string in it scrubbed, its shape kept: a transcript, an event payload."""
    if isinstance(value, str):
        return scrub_held_secrets(value)
    if isinstance(value, dict):
        return {key: scrub_held_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_held_value(item) for item in value]
    return value


def scrub_tool_output(arguments: Any, text: str) -> str:
    """A tool result with the credentials it could carry taken out.

    A read of a dotfile config under home loses its key values, and any value
    Raven itself holds is replaced by where it is kept. Every loop that hands a
    tool result to a model -- the main turn and a sub-agent's -- goes through
    this, so neither is the one that forgot.
    """
    return scrub_held_secrets(redact_home_config_read(arguments, text))


def scrub_tool_blocks(arguments: Any, blocks: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """The text parts of a multimodal tool result scrubbed like :func:`scrub_tool_output`; pictures pass as they are.

    A model that carries images in a tool result is sent these blocks instead of
    the text, so scrubbing the text alone would leave the same key in the half
    the model actually reads.
    """
    if not blocks:
        return blocks
    return [
        {**block, "text": scrub_tool_output(arguments, block["text"])}
        if block.get("type") == "text" and isinstance(block.get("text"), str)
        else block
        for block in blocks
    ]


__all__ = ["held_secrets", "scrub_held_secrets", "scrub_held_value", "scrub_tool_blocks", "scrub_tool_output"]
