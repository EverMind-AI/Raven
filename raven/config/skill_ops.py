"""Skill operations for the web skills page (Req2/Req3, phase 1 — A tier).

Read a skill's body, and drive the Skill Hub (test / search / install) from a
web RPC handler, reusing ``SkillHubClient`` and the local skill catalog. These
are read/fetch operations; config writes live in ``update_skills.py``.

Hub search/install operate on the *configured* hub (``skillForge.router.hub``);
``hub_test`` additionally accepts an endpoint/api_key override so the UI can
verify unsaved form values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from raven.config.loader import load_config


def _catalog():
    """Fresh, watcher-less catalog that re-scans disk on every call, so a
    just-installed hub skill shows up immediately without invalidation."""
    from raven.memory_engine.skill_forge import LocalSkillCatalog

    config = load_config()
    return LocalSkillCatalog(
        config.workspace_path,
        config=getattr(config, "skill_forge", None),
        start_watcher=False,
    )


def _hub_settings() -> tuple[str | None, str | None, float, Path]:
    """(endpoint, api_key, timeout_s, cache_dir) from the active config.

    Hub settings live in the ``skillForge`` extension block, so they must come
    from ``load_raven_config`` — the base ``Config.skill_forge`` is only a
    default stub and would report no endpoint. ``workspace_path`` is a base
    field, absent on ``RavenConfig``, so the cache dir still comes from
    ``load_config``.
    """
    from raven.config.raven import load_raven_config

    rc = load_raven_config()
    sf = getattr(rc, "skill_forge", None)
    router = getattr(sf, "router", None)
    hub = getattr(router, "hub", None)
    endpoint = getattr(hub, "endpoint", None)
    api_key = getattr(hub, "api_key", None)
    timeout_s = float(getattr(hub, "timeout_s", 2.0) or 2.0)
    cache_dir = Path(load_config().workspace_path) / "skills" / "hub"
    return endpoint, api_key, timeout_s, cache_dir


def _build_client(endpoint: str | None = None, api_key: str | None = None):
    """Build a ``SkillHubClient`` for the configured hub. ``endpoint``/``api_key``
    override the config (empty -> fall back to config). Returns ``None`` when no
    endpoint is available. Injection point monkeypatched by tests."""
    ep_cfg, key_cfg, timeout_s, cache_dir = _hub_settings()
    ep = endpoint or ep_cfg
    if not ep:
        return None
    key = api_key or key_cfg
    from raven.skill_hub.client import SkillHubClient

    return SkillHubClient(ep, api_key=key, timeout_s=timeout_s, cache_dir=cache_dir)


def _skill_dir_files(skill_dir: Path) -> list[str]:
    """Relative paths of all files under a skill folder (hidden dirs skipped),
    so the web detail page can render the same file-structure tree as hub skills."""
    out: list[str] = []
    if not skill_dir.is_dir():
        return out
    for f in skill_dir.rglob("*"):
        rel = f.relative_to(skill_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue  # skip .git, dotfiles, etc.
        if f.is_file():
            out.append(str(rel))
    return sorted(out)


def read_local_body(
    *,
    name: str | None = None,
    skill_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any] | None:
    """Return a local/installed skill's body plus the same shape of metadata the
    hub detail view uses (files / tags / description / category / license /
    source_url), so ``installed`` detail pages match ``hub`` ones. ``None`` if no
    skill matches the given id/name(+source)."""
    svc = _catalog()
    for m in svc.gather_all_skills():
        if skill_id and m.id != skill_id:
            continue
        if name and m.name != name:
            continue
        if source and m.source != source:
            continue
        body = m.content or ""
        if not body and m.path and Path(m.path).exists():
            body = Path(m.path).read_text(encoding="utf-8")
        fm = getattr(m, "raw_frontmatter", None) or {}
        tags = fm.get("tags") or fm.get("scenario_tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        skill_dir = Path(m.path).parent if m.path else None
        return {
            "skillMd": body,
            "name": m.name,
            "source": m.source,
            "path": str(m.path),
            "description": getattr(m, "description", "") or fm.get("description") or "",
            "category": fm.get("category") or fm.get("scenario") or None,
            "license": getattr(m, "license", None),
            "tags": list(tags),
            "source_url": fm.get("source_url") or fm.get("repo") or fm.get("homepage") or None,
            "files": _skill_dir_files(skill_dir) if skill_dir else [],
        }
    return None


async def hub_get_body(skill_id: str) -> dict[str, Any]:
    """Fetch a hub skill's body (``skill_md``) via ``GET /skills/{id}`` — no
    download. Returns ``{skillMd, name, version}``."""
    client = _build_client()
    if client is None:
        raise ValueError("no hub endpoint configured")
    try:
        meta = await client.get(skill_id)
        return {
            "skillMd": meta.get("skill_md", ""),
            "name": meta.get("name", ""),
            "version": str(meta.get("version") or ""),
        }
    finally:
        await client.aclose()


async def hub_test(*, endpoint: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    """Verify the hub is reachable with the given (or configured) credentials.
    Returns ``{ok, detail}`` — never raises."""
    client = _build_client(endpoint=endpoint, api_key=api_key)
    if client is None:
        return {"ok": False, "detail": "no hub endpoint configured"}
    try:
        # Probe with a non-empty query: some hub deployments require ``q`` and
        # 422 an empty one, which would misreport a reachable, authenticated hub
        # as down. A trivial term exercises the same transport + auth path; only
        # transport/auth failures raise and surface as ``ok: False``.
        await client.search("ping", limit=1)
        return {"ok": True, "detail": "connected"}
    except Exception as exc:  # noqa: BLE001 — surface any transport/envelope error as detail
        return {"ok": False, "detail": str(exc)}
    finally:
        await client.aclose()


async def hub_search(
    q: str,
    *,
    category: str | None = None,
    sort: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search / browse the configured hub catalog. Empty ``q`` -> sorted list."""
    client = _build_client()
    if client is None:
        raise ValueError("no hub endpoint configured")
    try:
        return await client.search(q or "", category=category, sort=sort, limit=limit)
    finally:
        await client.aclose()


async def hub_install(skill_id: str) -> dict[str, Any]:
    """Download + extract a hub skill into ``<workspace>/skills/hub`` (where the
    on-disk registry discovers it). Returns ``{ok, slug, version, dir}``."""
    client = _build_client()
    if client is None:
        raise ValueError("no hub endpoint configured")
    try:
        res = await client.install(skill_id)
        return {
            "ok": True,
            "slug": res.get("slug"),
            "version": res.get("version"),
            "dir": res.get("dir"),
        }
    finally:
        await client.aclose()


def _find_skill(name: str | None, skill_id: str | None, source: str | None):
    """First catalog skill matching id/name(+source), or None."""
    for m in _catalog().gather_all_skills():
        if skill_id and m.id != skill_id:
            continue
        if name and m.name != name:
            continue
        if source and m.source != source:
            continue
        return m
    return None


def zip_local(
    *,
    name: str | None = None,
    skill_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Zip an installed/builtin skill's folder for download (parity with the hub
    ``download`` action). Returns ``{filename, b64}``. The folder comes from the
    trusted catalog, not user input, so there is no path-injection surface."""
    import base64
    import io
    import zipfile

    m = _find_skill(name, skill_id, source)
    if m is None or not m.path:
        raise ValueError(f"skill not found: {name or skill_id}")
    skill_dir = Path(m.path).parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in _skill_dir_files(skill_dir):
            z.write(skill_dir / rel, arcname=f"{m.name}/{rel}")
    return {"filename": m.name, "b64": base64.b64encode(buf.getvalue()).decode("ascii")}


def remove_installed(
    *,
    name: str | None = None,
    skill_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Delete an installed skill's folder from ``<workspace>/skills/hub``.

    Only skills that live under the hub cache dir are removable — builtin and
    local-dir skills are refused, so this can never delete something Raven ships
    with or a directory the user pointed at. Resolves the on-disk path via the
    catalog, verifies it sits inside the hub cache dir, then removes the skill's
    containing folder. Returns ``{ok, removed}``.
    """
    import shutil

    _, _, _, cache_dir = _hub_settings()
    cache_root = cache_dir.resolve()

    match = None
    for m in _catalog().gather_all_skills():
        if skill_id and m.id != skill_id:
            continue
        if name and m.name != name:
            continue
        if source and m.source != source:
            continue
        match = m
        break
    if match is None or not match.path:
        raise ValueError(f"skill not found: {name or skill_id}")

    p = Path(match.path).resolve()
    skill_dir = p.parent if p.is_file() else p  # SKILL.md -> its folder
    try:
        skill_dir.relative_to(cache_root)
    except ValueError as exc:  # outside the hub cache dir -> not an installed skill
        raise ValueError(
            f"refusing to remove '{match.name}': not an installed hub skill (lives outside {cache_root})"
        ) from exc
    if skill_dir == cache_root:
        raise ValueError("refusing to remove the hub cache root itself")
    shutil.rmtree(skill_dir)
    return {"ok": True, "removed": str(skill_dir)}


__all__ = [
    "read_local_body",
    "hub_get_body",
    "hub_test",
    "hub_search",
    "hub_install",
    "remove_installed",
    "zip_local",
]
