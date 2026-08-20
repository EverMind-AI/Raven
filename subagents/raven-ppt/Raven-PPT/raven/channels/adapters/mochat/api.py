"""HTTP client for the Mochat REST API.

Owns the httpx session and the claw-token auth, exposes the shared POST +
error-unwrap and one coroutine per endpoint (session list / group list /
session watch / panel messages / session+panel send). The Socket.IO transport
stays in :mod:`.channel`. Live network flows, integration/manual tested.
"""

from __future__ import annotations

from typing import Any

import httpx

from raven.config.schema import MochatConfig


class MochatAPI:
    """Thin async wrapper over the Mochat `/api/claw/*` endpoints."""

    def __init__(self, config: MochatConfig):
        self.config = config
        self._http: httpx.AsyncClient | None = None

    async def open(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a Mochat endpoint and unwrap its `{code, data}` envelope."""
        if not self._http:
            raise RuntimeError("Mochat HTTP client not initialized")
        url = f"{self.config.base_url.strip().rstrip('/')}{path}"
        response = await self._http.post(
            url,
            headers={"Content-Type": "application/json", "X-Claw-Token": self.config.claw_token},
            json=payload,
        )
        if not response.is_success:
            raise RuntimeError(f"Mochat HTTP {response.status_code}: {response.text[:200]}")
        try:
            parsed = response.json()
        except Exception:
            parsed = response.text
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
            if parsed["code"] != 200:
                msg = str(parsed.get("message") or parsed.get("name") or "request failed")
                raise RuntimeError(f"Mochat API error: {msg} (code={parsed['code']})")
            data = parsed.get("data")
            return data if isinstance(data, dict) else {}
        return parsed if isinstance(parsed, dict) else {}

    # ── endpoints ─────────────────────────────────────────────────────

    async def list_sessions(self) -> dict[str, Any]:
        return await self.post("/api/claw/sessions/list", {})

    async def get_groups(self) -> dict[str, Any]:
        return await self.post("/api/claw/groups/get", {})

    async def watch_session(self, session_id: str, cursor: int, timeout_ms: int, limit: int) -> dict[str, Any]:
        return await self.post(
            "/api/claw/sessions/watch",
            {
                "sessionId": session_id,
                "cursor": cursor,
                "timeoutMs": timeout_ms,
                "limit": limit,
            },
        )

    async def panel_messages(self, panel_id: str, limit: int) -> dict[str, Any]:
        return await self.post("/api/claw/groups/panels/messages", {"panelId": panel_id, "limit": limit})

    async def send_session(self, session_id: str, content: str, reply_to: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"sessionId": session_id, "content": content}
        if reply_to:
            body["replyTo"] = reply_to
        return await self.post("/api/claw/sessions/send", body)

    async def send_panel(
        self, panel_id: str, content: str, reply_to: str | None = None, group_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"panelId": panel_id, "content": content}
        if reply_to:
            body["replyTo"] = reply_to
        if group_id:
            body["groupId"] = group_id
        return await self.post("/api/claw/groups/panels/send", body)
