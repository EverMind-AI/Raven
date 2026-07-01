"""HTTP client for the Skill Hub OpenAPI surface.

Endpoints (uniform envelope ``{error, requestId, status, result}``; a
response is successful only when ``error == "ok"`` and ``status == 0``):

- ``GET /openapi/v1/skills`` — semantic search; ``result.items[]`` metadata.
- ``GET /openapi/v1/skills/{id}`` — full metadata **+ ``skill_md``** (body).
- ``GET /openapi/v1/skills/{id}/download`` — raw zip bytes (NOT enveloped).

Design split (see docs/skill-hub-integration-design.md):

- :meth:`search` → discovery (catalog metadata, no body).
- :meth:`get` → read the body (``skill_md``) for fine-selection / pure-
  instruction execution — a cheap GET, **no download**.
- :meth:`download` / :meth:`install` → fetch the zip (bundled scripts /
  assets) and extract locally — only for skills that ship runnable files.
"""

from __future__ import annotations

import io
import logging
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 2.0
# Defensive limits for untrusted zip extraction.
_MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024  # 8 MiB per file
_MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MiB uncompressed total
_ALLOWED_SUFFIXES = {
    # docs / data / config
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".cfg",
    ".ini",
    ".xml",
    ".html",
    ".htm",
    ".sql",
    ".env",
    "",
    # scripts
    ".sh",
    ".py",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".rb",
    ".pl",
    ".lua",
    ".ps1",
    ".bat",
    # inert assets
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
}


class SkillHubError(RuntimeError):
    """Hub returned a non-ok envelope or a malformed/unsafe payload."""


class SkillHubClient:
    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        source: str = "raven",
        cache_dir: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = endpoint.rstrip("/")
        self._api_key = api_key
        self._source = source
        self._cache_dir = cache_dir or (Path.home() / ".raven" / "skills" / "hub")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"X-Request-ID": uuid.uuid4().hex}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    # Success markers seen across Hub deployments: ``"ok"`` (per the
    # original spec) and ``"success"`` (dev/aws). ``status == 0`` is the
    # authoritative signal; the string is accepted leniently.
    _OK_TOKENS = frozenset({"ok", "success"})

    @classmethod
    def _result(cls, payload: dict[str, Any]) -> Any:
        """Unwrap the uniform envelope; raise on a non-ok response."""
        if payload.get("error") not in cls._OK_TOKENS or payload.get("status") != 0:
            raise SkillHubError(
                f"hub error={payload.get('error')!r} status={payload.get('status')!r}",
            )
        return payload.get("result", {})

    # ── Discovery (catalog metadata) ────────────────────────────────
    async def search(
        self,
        q: str,
        *,
        category: str | None = None,
        sort: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if q:
            params["q"] = q
        if category:
            params["category"] = category
        if sort:
            params["sort"] = sort
        r = await self._client.get(
            f"{self._base}/openapi/v1/skills",
            params=params,
            headers=self._headers(),
        )
        r.raise_for_status()
        result = self._result(r.json() or {})
        return list(result.get("items", []))

    # ── Read body (skill_md) — no download ──────────────────────────
    async def get(self, skill_id: str) -> dict[str, Any]:
        r = await self._client.get(
            f"{self._base}/openapi/v1/skills/{skill_id}",
            headers=self._headers(),
        )
        r.raise_for_status()
        return dict(self._result(r.json() or {}))

    # ── Bundle (zip with scripts/assets) ────────────────────────────
    async def download(self, skill_id: str) -> bytes:
        r = await self._client.get(
            f"{self._base}/openapi/v1/skills/{skill_id}/download",
            params={"source": self._source},
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.content

    async def install(
        self,
        skill_id: str,
        *,
        prefetched_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Download + safely extract the zip into the local cache.

        Returns ``{slug, version, dir, scripts_dir, skill_md}``. Cache hit
        on ``<slug>@<version>`` skips the re-download.

        ``prefetched_meta`` skips the internal ``self.get(skill_id)``
        round-trip when the caller already has the metadata (e.g. the
        SkillsSegmentBuilder pre-gate body hydrate). This shaves
        ~50-200ms off post-gate hydrate per selected Hub hit.
        """
        meta = prefetched_meta if prefetched_meta is not None else await self.get(skill_id)
        slug = meta.get("slug") or meta.get("skill_id") or skill_id
        slug = str(slug).replace("/", "_")
        version = str(meta.get("version") or "v0")
        dest = self._cache_dir / f"{slug}@{version}"
        if not dest.exists():
            self._safe_extract(await self.download(skill_id), dest)
        root = self._bundle_root(dest)
        scripts = root / "scripts"
        return {
            "slug": slug,
            "version": version,
            "dir": str(root),
            "scripts_dir": str(scripts) if scripts.is_dir() else None,
            "skill_md": meta.get("skill_md", ""),
        }

    @staticmethod
    def _bundle_root(dest: Path) -> Path:
        """Resolve the real skill directory inside the extracted bundle.

        Hub zips wrap the whole skill in a single ``<skill>/`` directory, so
        ``SKILL.md`` / ``scripts/`` live one level below ``dest``. Collapse
        that lone wrapper; a flat zip (multiple top-level entries, or any
        top-level file) keeps ``dest`` as the root.
        """
        try:
            entries = [p for p in dest.iterdir() if not p.name.startswith(".")]
        except OSError:
            return dest
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return dest

    @staticmethod
    def _safe_extract(zip_bytes: bytes, dest: Path) -> None:
        """Extract a zip, hard-rejecting path traversal and skipping
        otherwise-unsafe entries (disallowed type / oversized) rather than
        failing the whole bundle — one stray asset shouldn't make an entire
        skill uninstallable."""
        dest.mkdir(parents=True, exist_ok=True)
        total = 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                target = (dest / name).resolve()
                # Path traversal is a security boundary, never tolerated.
                if not str(target).startswith(str(dest.resolve()) + "/"):
                    raise SkillHubError(f"unsafe zip path: {name!r}")
                if Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
                    logger.warning("skipping disallowed file in skill zip: %r", name)
                    continue
                if info.file_size > _MAX_ZIP_ENTRY_BYTES:
                    logger.warning("skipping oversized file in skill zip: %r", name)
                    continue
                if total + info.file_size > _MAX_ZIP_TOTAL_BYTES:
                    raise SkillHubError("zip uncompressed total too large")
                total += info.file_size
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))


__all__ = ["SkillHubClient", "SkillHubError"]
