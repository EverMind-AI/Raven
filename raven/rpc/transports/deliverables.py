"""Deliverable download routes for the page transport.

The served page (``raven serve`` standalone or the gateway's page mount)
lets a user fetch a deliverable the agent produced. The routes hand a file
out by opaque token and answer 410 for a token whose deliverable is gone
versus 404 for one that never existed, so the client can tell "expired"
from "never". Mounted by :func:`raven.rpc.transports.ws.build_app` behind
that transport's origin + session guard; nothing else serves them.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from collections.abc import Callable
from urllib.parse import quote

from aiohttp import web
from loguru import logger

from raven.agent.tools.deliverables import DeliverableRecord, DeliverableStore
from raven.config.paths import get_cache_dir

_CHUNK = 64 * 1024


def resolve_download(store: DeliverableStore, token: str | None) -> DeliverableRecord | None:
    """Resolve a token to a still-present file, dropping a stale entry."""
    if not token:
        return None
    record = store.get(token)
    if record is None:
        return None
    if not os.path.isfile(record.path):
        store.drop(token)
        return None
    return record


def _attachment(name: str) -> str:
    """Build an RFC 6266 Content-Disposition for a delivered file.

    The value must survive a latin-1 header encode: the web service relays this
    response through Starlette, which raises on a non-ASCII header value and
    turns a perfectly good download into a 500. So ``filename`` carries an
    ASCII-only fallback and the real name travels percent-encoded in
    ``filename*``. Control characters are stripped here rather than left for
    aiohttp's header validator to reject with a 500.
    """
    cleaned = "".join(ch for ch in name if ch.isprintable()).replace('"', "").replace("\\", "")
    fallback = "".join(ch if ch.isascii() else "_" for ch in cleaned).strip() or "download"
    header = f'attachment; filename="{fallback}"'
    if cleaned and cleaned != fallback:
        header += f"; filename*=UTF-8''{quote(cleaned, safe='')}"
    return header


def _unique_arcname(name: str, taken: set[str]) -> str:
    """Disambiguate a colliding archive entry as ``report (2).pdf``.

    Two deliverables from different directories can share a basename; without
    this the second silently overwrites the first on extraction.
    """
    if name not in taken:
        taken.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot and stem else (name, "")
    index = 2
    while f"{base} ({index}){suffix}" in taken:
        index += 1
    candidate = f"{base} ({index}){suffix}"
    taken.add(candidate)
    return candidate


def _build_archive(tmp_name: str, records: list[DeliverableRecord]) -> None:
    """Compress the delivery into ``tmp_name``. Runs in a worker thread: delivery
    size is uncapped, so deflating on the event loop would stall every live turn
    the gateway is streaming."""
    taken: set[str] = set()
    with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as archive:
        for record in records:
            archive.write(record.path, arcname=_unique_arcname(record.name, taken))


def add_files_routes(
    app: web.Application,
    store: DeliverableStore | None,
    *,
    guard: Callable[[web.Request], None] | None = None,
) -> None:
    """Register the deliverable download routes. A None store registers nothing,
    so the gateway's control port exposes no download surface at all."""
    if store is None:
        return

    async def download(request: web.Request) -> web.StreamResponse:
        if guard is not None:
            guard(request)
        token = request.query.get("token")
        if not token:
            raise web.HTTPBadRequest(text="token is required")
        record = resolve_download(store, token)
        if record is None:
            raise web.HTTPGone(text="deliverable is gone")
        return web.FileResponse(
            record.path,
            headers={
                "Content-Type": record.media_type,
                "Content-Disposition": _attachment(record.name),
            },
        )

    async def download_archive(request: web.Request) -> web.StreamResponse:
        if guard is not None:
            guard(request)
        tokens = request.query.getall("token", [])
        records = [r for r in (resolve_download(store, t) for t in tokens) if r is not None]
        if not records:
            raise web.HTTPGone(text="deliverables are gone")

        response = web.StreamResponse(
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": _attachment("deliverables.zip"),
            }
        )
        if request.method == "HEAD":
            await response.prepare(request)
            await response.write_eof()
            return response

        tmp_dir = get_cache_dir() / "deliverables"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
        os.close(handle)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _build_archive, tmp_name, records)
            await response.prepare(request)
            with open(tmp_name, "rb") as fh:
                while chunk := fh.read(_CHUNK):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.warning("deliverables: could not remove temp archive {}", tmp_name)

    app.router.add_get("/files/download", download, allow_head=True)
    app.router.add_get("/files/download-archive", download_archive, allow_head=True)
