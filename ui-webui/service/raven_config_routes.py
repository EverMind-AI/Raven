"""REST proxy for Raven config admin (P4).

The web service env has no ``raven``, so config admin lives on the gateway
(validated + hot-applied). These routes proxy the browser's REST calls to the
gateway's web-channel RPC over the shared GatewayClient WebSocket.

Mounted by main.py only when RAVEN_GATEWAY is on.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from raven_gateway_agent import GatewayClient, gateway_http_base

_FORWARDED_HEADERS = ("content-type", "content-disposition", "content-length")

# Strong references to in-flight direct-chat drivers. asyncio keeps only a weak
# one, so without this a turn can be garbage-collected mid-answer.
_DIRECT_TASKS: set[asyncio.Task] = set()


def _session_key(session_id: str) -> str:
    """The raven session key for a web session id.

    One derivation, matching ``RavenGatewayAgent.reply_stream``: the instance
    registry, the direct-chat records and the handoff are all keyed by it, so a
    second spelling would address a session that holds none of them.
    """
    return f"web:{session_id}" if session_id else "web:default"


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

    @router.get("/subagents/probe")
    async def probe_subagents() -> dict:
        client = await GatewayClient.shared()
        # Worst case is partly sequential: PATH capture up to 15s (login_shell_env's
        # first bash -lic), then concurrent HTTP probes up to 10s -- longer than the
        # 30s default is comfortable with on a slow profile.
        return await client.call("raven.subagents.probe", {}, timeout=60)

    @router.post("/subagents/test")
    async def test_subagent(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        # probe.py's TEST_TIMEOUT_SECONDS (120) caps the run itself; add room for
        # spawn and transport so that cap is actually reachable over this hop
        # rather than being cut off by the 30s default first.
        return await client.call(
            "raven.subagents.test",
            {"name": body.get("name") or "", "source": body.get("source") or "config"},
            timeout=150,
        )

    @router.get("/subagents/instances")
    async def list_subagent_instances(session_key: str | None = None) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.instances", {"session_key": session_key})

    @router.delete("/subagents/instances")
    async def delete_subagent_instances(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.subagents.instances.delete", {"session_key": session_key})

    @router.get("/subagents/instances/history")
    async def instance_history(session_id: str, agent: str, handle: str) -> dict:
        """One instance's past direct turns, for a view being (re)opened.

        The records on disk are the only memory of a direct chat: it is
        deliberately absent from the session transcript, and its live events are
        published out of band rather than stored, so a reload has nowhere else
        to read it from.
        """
        client = await GatewayClient.shared()
        return await client.call(
            "subagents.instance.history",
            {"session_key": _session_key(session_id), "agent": agent, "handle": handle},
        )

    @router.post("/subagents/instances/chat")
    async def instance_chat(payload: dict = Body(...)) -> dict:
        """Send one prompt to a sub-agent instance and stream its reply out of band.

        Returns as soon as the turn is *accepted*, not when it is answered: the
        reply arrives as `subagent_direct_*` custom events on the session's SSE,
        which is what lets several instances answer at once without any of them
        holding an HTTP request open.
        """
        session_id = str(payload.get("session_id") or "")
        agent = str(payload.get("agent") or "")
        handle = str(payload.get("handle") or "")
        content = str(payload.get("content") or "")
        if not (session_id and agent and handle and content):
            raise HTTPException(status_code=400, detail="session_id, agent, handle and content are required")

        from raven_gateway_agent import publish_session_custom, run_direct_chat

        async def _publish(name: str, value: dict) -> None:
            await publish_session_custom(session_id, name, value)

        # Detached deliberately, and referenced until it ends: a direct turn runs
        # as long as the sub-agent takes, and awaiting it here would tie that to
        # one HTTP request that a page reload would abandon mid-answer.
        task = asyncio.create_task(run_direct_chat(_session_key(session_id), agent, handle, content, publish=_publish))
        _DIRECT_TASKS.add(task)
        task.add_done_callback(_DIRECT_TASKS.discard)
        return {"accepted": True}

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
    async def get_dag_node(
        run_id: str,
        node_id: str,
        max_output_chars: int = 20000,
        session_key: str | None = None,
    ) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.subagents.dag.node",
                {
                    "run_id": run_id,
                    "node": node_id,
                    "max_output_chars": max_output_chars,
                    "session_key": session_key,
                },
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

    @router.get("/sessions/{session_key}/workdir")
    async def get_session_workdir(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.session.workdir.get", {"session_key": session_key})

    @router.put("/sessions/{session_key}/workdir")
    async def set_session_workdir(session_key: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.session.workdir.set",
                {"session_key": session_key, "workdir": body.get("workdir")},
            )
        except Exception as exc:  # invalid path / busy session / transport
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

    @router.get("/mcp")
    async def list_mcp() -> dict:
        """Raven's own MCP servers, with live connection state and tool names.

        Not to be confused with ``/workspace/mcp``, which is the AgentScope
        workspace's list -- that store is not wired to the gateway agent, so it
        describes servers this chat cannot reach.
        """
        client = await GatewayClient.shared()
        return await client.call("raven.mcp.list", {})

    @router.put("/mcp")
    async def set_mcp(body: dict = Body(...)) -> dict:
        """Replace the MCP server list. Returns ``restart_required``: MCP tools
        are registered once at connect time, so the change lands on the next
        gateway restart rather than immediately."""
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.mcp.set", {"servers": body.get("servers") or []})
        except Exception as exc:  # validation / transport
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

    @router.get("/everos")
    async def get_everos() -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.everos.get", {})
        except Exception as exc:  # transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/everos/providers")
    async def everos_providers() -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.everos.providers", {})
        except Exception as exc:  # transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/everos/models")
    async def everos_models(body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.everos.models",
                {
                    "section": body.get("section", "llm"),
                    "base_url": body.get("base_url"),
                    "api_key": body.get("api_key"),
                    "provider_name": body.get("provider_name"),
                    "reuse_key_from": body.get("reuse_key_from"),
                    "credential_provider": body.get("credential_provider"),
                },
            )
        except Exception as exc:  # transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/everos/{section}")
    async def set_everos(section: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.everos.set",
                {
                    "section": section,
                    "fields": body.get("fields") or {},
                    "reuse_key_from": body.get("reuse_key_from"),
                    "credential_provider": body.get("credential_provider"),
                },
            )
        except Exception as exc:  # unknown section / validation / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/everos/{section}")
    async def clear_everos(section: str) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call("raven.everos.clear", {"section": section})
        except Exception as exc:  # unknown section / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/everos/{section}/test")
    async def test_everos(section: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.everos.test",
                {
                    "section": section,
                    "fields": body.get("fields") or {},
                    "reuse_key_from": body.get("reuse_key_from"),
                    "credential_provider": body.get("credential_provider"),
                },
            )
        except Exception as exc:  # transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
