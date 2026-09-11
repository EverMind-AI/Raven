"""Credentials a machine holds for one playbook's carried MCP servers.

A playbook file is a distribution unit, so the ``secret`` params its
``mcpServers`` reference (``{{ params.TOKEN }}`` in a header) and the OAuth
tokens its ``auth: oauth`` servers need cannot live beside the definitions.
They live here instead, keyed by playbook name, under the same credentials
root the host's own MCP OAuth tokens use::

    <credentials>/playbooks/<playbook>/params.json        {"TOKEN": "..."}
    <credentials>/playbooks/<playbook>/mcp/<server>.json  (FileTokenStorage, scoped)

Scoped by playbook rather than by bare server name because a carried server
may shadow a host server of the same name (``dag_mcp_scope``): keyed by name
alone, a carried ``sentry`` would read and overwrite the host's ``sentry.json``.

Only ``secret`` params are read back. A value stored for a name the spec does
not declare as ``secret`` is ignored, so a stored file cannot widen what a
playbook's author let a run supply.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.playbook.params import secret_param_names
from raven.utils.atomic_io import atomic_update, remove_with_lock
from raven.utils.paths import safe_path_segment

if TYPE_CHECKING:
    from raven.playbook.types import PlaybookSpec

_PARAMS_FILE = "params.json"


def credential_scope(playbook: str) -> str:
    """The scope token ``raven.mcp.oauth.credentials_path`` takes for this playbook."""
    return f"playbooks/{safe_path_segment(playbook)}"


def playbook_credentials_dir(playbook: str) -> Path:
    """Where this playbook's credentials live. Not created here: reads must not leave directories behind."""
    from raven.config.paths import get_runtime_subdir

    return get_runtime_subdir("credentials") / credential_scope(playbook)


def _params_path(playbook: str) -> Path:
    return playbook_credentials_dir(playbook) / _PARAMS_FILE


def _parse(raw: str | None, where: Path | None = None) -> dict[str, Any]:
    """A credentials file's content as a dict; empty for a missing, blank, or unreadable one."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        if where is not None:
            logger.warning("playbook credentials: unreadable {} ({})", where, e)
        return {}
    return data if isinstance(data, dict) else {}


def _read_params(playbook: str) -> dict[str, str]:
    path = _params_path(playbook)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return {str(k): str(v) for k, v in _parse(raw, path).items()}


def stored_secret_params(spec: "PlaybookSpec") -> dict[str, str]:
    """The ``secret`` params of ``spec`` this machine has a value for."""
    wanted = secret_param_names(spec)
    if not wanted:
        return {}
    return {k: v for k, v in _read_params(spec.name).items() if k in wanted and v}


def stored_secret_param_names(playbook: str) -> frozenset[str]:
    """Which params have a stored value -- names only, never the values."""
    return frozenset(k for k, v in _read_params(playbook).items() if v)


def set_secret_param(playbook: str, name: str, value: str) -> None:
    if not value:
        clear_secret_param(playbook, name)
        return
    path = _params_path(playbook)

    def _apply(current: str | None) -> tuple[str, None]:
        data = _parse(current)
        data[name] = value
        return json.dumps(data, indent=2, sort_keys=True) + "\n", None

    # Anchored at 0600 before the first write, the way FileTokenStorage does it:
    # atomic_update carries an existing target's mode across the replace, so the
    # mode has to exist before the value does.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT, 0o600))
    atomic_update(path, _apply)
    os.chmod(path, 0o600)


def clear_secret_param(playbook: str, name: str) -> None:
    path = _params_path(playbook)
    if not path.exists():
        return

    def _apply(current: str | None) -> tuple[str | None, None]:
        data = _parse(current)
        if name not in data:
            return None, None
        data.pop(name)
        return (json.dumps(data, indent=2, sort_keys=True) + "\n") if data else "", None

    atomic_update(path, _apply)


def has_oauth_tokens(server: str, playbook: str) -> bool:
    """Whether this machine holds an access token for ``server`` under ``playbook``.

    A file read, never a request: the gate that asks this runs on every dispatch,
    and token validity is the provider's business when it dials.
    """
    from raven.mcp.oauth import has_stored_tokens

    return has_stored_tokens(server, scope=credential_scope(playbook))


def clear_oauth_tokens(server: str, playbook: str) -> None:
    from raven.mcp.oauth import credentials_path

    remove_with_lock(credentials_path(server, scope=credential_scope(playbook)))


__all__ = [
    "clear_oauth_tokens",
    "clear_secret_param",
    "credential_scope",
    "has_oauth_tokens",
    "playbook_credentials_dir",
    "set_secret_param",
    "stored_secret_param_names",
    "stored_secret_params",
]
