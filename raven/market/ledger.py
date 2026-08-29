"""Install ledger — one JSON file per PlugHub-installed plugin.

The ledger is what makes a multi-piece install reversible: it records the
exact pieces the transaction landed (config key, credentials path, skill
directory), so uninstall replays it in reverse and never guesses. It is
also the provenance oracle: a PlugHub install writes a ledger, so a config
entry with a ledger -> "market", without -> "manual".

Layout (``~/.raven/plugins/<catalog_id>.json``):

    {
      "catalog_id": "notion", "catalog_version": "1.2.0",
      "installed_at": "2026-08-05T12:00:00Z",
      "pieces": [
        {"kind": "mcp", "config_key": "tools.mcp_servers.notion"},
        {"kind": "skill", "name": "notion-usage"}
      ]
    }
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from raven.utils.atomic_io import atomic_replace


def _plugins_dir() -> Path:
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("plugins")


# A catalogue id names a file, so it may not be able to name a *place*. Both the
# hosted catalogue and the RPC caller supply these ids, and `Path.__truediv__`
# honours both `..` and an absolute string -- so without this an id of
# `../config` resolves to ~/.raven/config.json, which uninstall would then
# happily unlink, and `../credentials/mcp/github` would take the user's stored
# OAuth refresh token with it.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class LedgerIdError(ValueError):
    """The catalogue id could not be used as a filename."""


def validate_catalog_id(catalog_id: str) -> str:
    """Return the id unchanged, or raise :class:`LedgerIdError`."""
    if not _ID_RE.match(catalog_id or ""):
        raise LedgerIdError(f"not a usable catalog id: {catalog_id!r}")
    return catalog_id


def ledger_path(catalog_id: str) -> Path:
    validate_catalog_id(catalog_id)
    root = _plugins_dir()
    path = root / f"{catalog_id}.json"
    # Belt and braces: the regex already forbids separators, so a mismatch here
    # means the pattern was loosened without this check being revisited.
    if path.resolve().parent != root.resolve():
        raise LedgerIdError(f"catalog id escapes the ledger directory: {catalog_id!r}")
    return path


def read_ledger(catalog_id: str) -> dict | None:
    """The ledger for ``catalog_id``, or None when there is not one.

    An id that cannot name a file provably has no ledger, so it answers None
    rather than raising: a ledger read is a question ("did PlugHub install
    this?"), and the answer for a hand-written server name -- which is never
    used as a filename -- is no, not an error about a term the user never used.
    """
    try:
        return json.loads(ledger_path(catalog_id).read_text(encoding="utf-8"))
    except LedgerIdError:
        return None
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("plughub: unreadable ledger for '{}' ({})", catalog_id, e)
        return None


def read_ledgers() -> dict[str, dict]:
    """All ledgers, keyed by catalog id."""
    out: dict[str, dict] = {}
    root = _plugins_dir()
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.json")):
        led = read_ledger(p.stem)
        if led is not None:
            out[p.stem] = led
    return out


def write_ledger(catalog_id: str, catalog_version: str, pieces: list[dict]) -> None:
    path = ledger_path(catalog_id)
    data = {
        "catalog_id": catalog_id,
        "catalog_version": catalog_version,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pieces": pieces,
    }
    atomic_replace(path, json.dumps(data, ensure_ascii=False, indent=2))


def delete_ledger(catalog_id: str) -> None:
    ledger_path(catalog_id).unlink(missing_ok=True)


__all__ = [
    "LedgerIdError",
    "delete_ledger",
    "ledger_path",
    "read_ledger",
    "read_ledgers",
    "validate_catalog_id",
    "write_ledger",
]
