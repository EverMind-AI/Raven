"""EverOS storage scope: the ``(app_id, project_id)`` pair.

EverOS partitions memory on disk as
``<root>/<app_id>/<project_id>/{users,agents}/...`` and a search never
crosses that pair, so one server process can serve many isolated
spaces. Raven uses ``project_id`` to keep one workspace's memory out of
another's while every workspace shares a single EverOS instance.

The pair is derived once per backend and stamped onto every request, so
the write side and the read side cannot address different buckets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_ID = "raven"

# EverOS validates both ids as 1-128 chars of ``[a-zA-Z0-9_.-]`` and
# rejects the literals "." and "..". A workspace path satisfies none of
# that on its own, so the slug is filtered and the digest carries the
# uniqueness.
_ALLOWED = re.compile(r"[^a-zA-Z0-9_.-]+")
_SLUG_MAX_CHARS = 32
_DIGEST_CHARS = 12


@dataclass(frozen=True)
class Scope:
    """One EverOS storage partition."""

    app_id: str
    project_id: str


def project_id_for_workspace(workspace: Path | str) -> str:
    """Derive a stable, EverOS-legal project id from a workspace path.

    The readable slug is for humans reading the directory tree; the
    digest is what makes it unique. Hashing rather than escaping is
    deliberate: the escape used for raven's own state dirs collapses
    distinct paths onto one name, which here would silently merge two
    repositories' memory.
    """
    resolved = str(Path(workspace).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    slug = _ALLOWED.sub("-", Path(resolved).name).strip("-.")[:_SLUG_MAX_CHARS]
    return f"ws-{slug}-{digest}" if slug else f"ws-{digest}"


def resolve_scope(
    workspace: Path | str | None,
    config: dict | None = None,
) -> Scope:
    """Resolve the scope for one backend instance.

    An explicit ``app_id`` / ``project_id`` in the plugin's config slice
    wins; otherwise the app is raven and the project follows the
    workspace. Without a workspace there is nothing to isolate by, so the
    project falls back to EverOS's own default bucket.
    """
    config = config or {}
    app_id = config.get("app_id") or DEFAULT_APP_ID
    project_id = config.get("project_id")
    if not project_id:
        project_id = project_id_for_workspace(workspace) if workspace else "default"
    return Scope(app_id=str(app_id), project_id=str(project_id))
