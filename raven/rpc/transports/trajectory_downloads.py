"""The trajectory export download route for the page transport.

A finished export is fetched by the random token its job was given; the token
is the whole address, so no path ever travels to or from the page. Like the
deliverable routes, a token that resolves to nothing answers 410 (gone, or
never a live export) and a request without one answers 400. Mounted by
:func:`raven.rpc.transports.ws.build_app` behind that transport's origin and
session guard.
"""

from __future__ import annotations

from collections.abc import Callable

from aiohttp import web

from raven.rpc.transports.deliverables import _attachment
from raven.trajectory.export_jobs import DOWNLOAD_PATH, ExportJobs


def add_export_routes(
    app: web.Application,
    jobs: ExportJobs | Callable[[], ExportJobs],
    *,
    guard: Callable[[web.Request], None] | None = None,
) -> None:
    read_jobs: Callable[[], ExportJobs] = jobs if callable(jobs) else lambda: jobs

    async def download(request: web.Request) -> web.StreamResponse:
        if guard is not None:
            guard(request)
        token = request.query.get("token")
        if not token:
            raise web.HTTPBadRequest(text="token is required")
        found = read_jobs().download(token)
        if found is None or not found[0].is_file():
            raise web.HTTPGone(text="export is gone")
        path, name = found
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/gzip",
                "Content-Disposition": _attachment(name),
                "Cache-Control": "no-store",
            },
        )

    app.router.add_get(DOWNLOAD_PATH, download, allow_head=True)


__all__ = ["add_export_routes"]
