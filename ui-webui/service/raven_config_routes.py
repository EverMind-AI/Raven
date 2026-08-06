"""REST proxy for Raven config admin (P4).

The web service env has no ``raven``, so config admin lives on the gateway
(validated + hot-applied). These routes proxy the browser's REST calls to the
gateway's web-channel RPC over the shared GatewayClient WebSocket.

Mounted by main.py only when RAVEN_GATEWAY is on.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from raven_gateway_agent import GatewayClient, gateway_http_base

_FORWARDED_HEADERS = ("content-type", "content-disposition", "content-length")


async def _proxy_files(request: Request, path: str) -> StreamingResponse:
    """Relay a token-addressed download from the gateway.

    The gateway is loopback-bound by design, so the browser cannot reach it
    directly whenever it is not on the gateway host. The service forwards the
    token and relays the stream; it resolves no paths and reads no files, so file
    access and authorization stay entirely on the gateway side. Both hops stream,
    so memory stays bounded no matter how large the deliverable is.
    """
    import aiohttp

    url = f"{gateway_http_base()}{path}"
    session = aiohttp.ClientSession()
    try:
        upstream = await session.request(request.method, url, params=request.query_params.multi_items())
    except aiohttp.ClientError as exc:
        await session.close()
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    except BaseException:
        # Covers asyncio.CancelledError (a Uvicorn graceful shutdown cancels the
        # in-flight request coroutine), which does not subclass ClientError: the
        # session must still be closed before the cancellation propagates.
        await session.close()
        raise

    if upstream.status >= 400:
        status = upstream.status
        upstream.release()
        await session.close()
        raise HTTPException(status_code=status, detail="deliverable not available")

    headers = {k: v for k, v in upstream.headers.items() if k.lower() in _FORWARDED_HEADERS}

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(body(), status_code=upstream.status, headers=headers)


# Full-catalog browse hits the hub's paginated ``/skills/search`` directly
# (the ``/skills`` endpoint the raven SkillHubClient uses is semantic-only,
# hard-capped at 10 results with no pagination). Mirrors the configured
# SkillForge hub endpoint; update if the hub moves.
_SKILLHUB_BASE = "https://skillhub.evermind.ai"


def build_raven_config_router() -> APIRouter:
    router = APIRouter(prefix="/raven", tags=["raven-config"])

    @router.api_route("/files/download", methods=["GET", "HEAD"])
    async def download_deliverable(request: Request):
        return await _proxy_files(request, "/files/download")

    @router.api_route("/files/download-archive", methods=["GET", "HEAD"])
    async def download_deliverable_archive(request: Request):
        return await _proxy_files(request, "/files/download-archive")

    @router.get("/subagents")
    async def list_subagents() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.list", {})

    @router.get("/subagents/presets")
    async def subagent_presets() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.presets", {})

    @router.get("/subagents/instances")
    async def list_subagent_instances(session_key: str | None = None) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.instances", {"session_key": session_key})

    @router.delete("/subagents/instances")
    async def delete_subagent_instances(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.instances.delete", {"session_key": session_key})

    @router.post("/subagents/dag/{run_id}/cancel")
    async def cancel_dag_run(run_id: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.dag.cancel", {"run_id": run_id})

    # Both DAG reads 404 rather than 500 on any failure: a run dir is pruned
    # with the workspace, and a malformed id names no run either, so "not
    # found" is the honest answer in both cases. The browser treats it as
    # "nothing to restore" and falls back to what the transcript alone carries.
    @router.get("/subagents/dag/{run_id}")
    async def get_dag_run(run_id: str, session_key: str | None = None) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.subagents.dag.get", {"run_id": run_id, "session_key": session_key})
        except Exception as exc:  # pruned run dir / bad id / transport
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/subagents/dag/{run_id}/nodes/{node_id}")
    async def get_dag_node(run_id: str, node_id: str, max_output_chars: int = 20000) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.subagents.dag.node",
                {"run_id": run_id, "node": node_id, "max_output_chars": max_output_chars},
            )
        except Exception as exc:  # pruned run dir / bad id / transport
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/subagents/instances/cancel")
    async def cancel_subagent_instance(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        return await client.call(
            "raven.subagents.instances.cancel",
            {
                "session_key": body.get("session_key", ""),
                "agent": body.get("agent", ""),
                "handle": body.get("handle", ""),
            },
        )

    @router.put("/subagents")
    async def set_subagents(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.subagents.set", {"agents": body.get("agents") or []})
        except Exception as exc:  # validation / duplicate-name / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/cron")
    async def list_cron() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.cron.list", {})

    @router.post("/cron")
    async def add_cron(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.cron.add", body)
        except Exception as exc:  # invalid schedule / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/cron/{job_id}")
    async def remove_cron(job_id: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.cron.remove", {"job_id": job_id})

    @router.get("/channels")
    async def list_channels() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.channels.list", {})

    @router.put("/channels/{name}")
    async def set_channel(name: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.channels.set", {"name": name, "fields": body.get("fields") or {}})
        except Exception as exc:  # unknown field / validation / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/gateway/restart")
    async def restart_gateway() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.gateway.restart", {})

    @router.get("/channels/{name}/qr")
    async def channel_qr(name: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.channels.qr", {"name": name})

    @router.get("/sessions/{session_key}/model")
    async def get_session_model(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.session.model.get", {"session_key": session_key})

    @router.put("/sessions/{session_key}/model")
    async def set_session_model(session_key: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.session.model.set",
                {"session_key": session_key, "model": body.get("model")},
            )
        except Exception as exc:  # unroutable model / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/skills")
    async def get_skills() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.skills.get", {})

    @router.put("/skills")
    async def set_skills(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.skills.set", {"fields": body.get("fields") or {}})
        except Exception as exc:  # validation / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/skills/available")
    async def list_skills() -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.skills.list", {})

    @router.post("/skills/body")
    async def skill_body(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.skills.body",
                {"name": body.get("name"), "id": body.get("id"), "source": body.get("source")},
            )
        except Exception as exc:  # not found / hub transport
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/skills/hub/test")
    async def hub_test(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        return await client.call(
            "raven.skills.hub.test",
            {"endpoint": body.get("endpoint"), "apiKey": body.get("apiKey")},
        )

    @router.get("/skills/hub/search")
    async def hub_search(q: str = "", limit: int = 20, sort: str | None = None) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.skills.hub.search", {"q": q, "limit": limit, "sort": sort})
        except Exception as exc:  # no endpoint / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/skills/hub/install")
    async def hub_install(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.skills.hub.install", {"id": body.get("id") or ""})
        except Exception as exc:  # no endpoint / unsafe zip / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/skills/remove")
    async def remove_skill(body: dict = Body(...)) -> dict:
        """Uninstall a downloaded skill (delete its folder under the hub cache).
        The gateway-side guard refuses anything outside ``<workspace>/skills/hub``,
        so builtin and local-dir skills can't be deleted here."""
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.skills.remove",
                {"name": body.get("name"), "id": body.get("id"), "source": body.get("source")},
            )
        except Exception as exc:  # not found / not removable / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/skills/download-local")
    async def download_local(name: str = "", source: str = "", id: str = "") -> Response:
        """Download an installed/builtin skill's folder as a .zip (parity with the
        hub download). The gateway zips the on-disk folder and returns base64."""
        import base64

        client = await GatewayClient.shared()
        try:
            res = await client.call(
                "raven.skills.zip",
                {"name": name or None, "source": source or None, "id": id or None},
            )
        except Exception as exc:  # not found / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        content = base64.b64decode(res.get("b64") or "")
        safe = "".join(ch for ch in (res.get("filename") or name or "skill") if ch.isalnum() or ch in "-_.")
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe or "skill"}.zip"'},
        )

    @router.get("/skills/hub/browse")
    async def hub_browse(
        page: int = 1,
        limit: int = 24,
        category: str | None = None,
        q: str | None = None,
    ) -> dict:
        """Paginated full-catalog browse (all ~93k skills), proxied to the hub's
        ``/openapi/v1/skills/search``. Returns ``{items, total, page, limit}``."""
        params: dict = {"page": page, "limit": limit}
        if category:
            params["category"] = category
        if q:
            params["q"] = q
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{_SKILLHUB_BASE}/openapi/v1/skills/search", params=params)
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        res = (data or {}).get("result") or {}
        return {
            "items": res.get("items", []),
            "total": res.get("total"),
            "page": res.get("page", page),
            "limit": res.get("limit", limit),
        }

    @router.get("/skills/hub/skill")
    async def hub_skill(id: str) -> dict:
        """Full metadata + ``skill_md`` for one hub catalog skill (detail view)."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{_SKILLHUB_BASE}/openapi/v1/skills/{id}")
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return (data or {}).get("result") or {}

    @router.get("/skills/hub/download")
    async def hub_download(id: str, name: str = "skill") -> Response:
        """Proxy the hub's skill .zip so the browser can download it without a
        cross-origin request to skillhub."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(f"{_SKILLHUB_BASE}/openapi/v1/skills/{id}/download")
                r.raise_for_status()
                content = r.content
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.") or "skill"
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
        )

    return router
