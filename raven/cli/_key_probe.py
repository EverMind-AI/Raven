"""Free ``GET /v1/models`` credential probe, shared by the CLI's key prompts.

Why ``/v1/models`` rather than a chat completion: it is a metadata endpoint, so
it costs no tokens and returns in milliseconds, and a provider that answers it
with 200 is one whose key is live. A chat "ping" would bill the user, and
against an agentic endpoint it would start real work.

Callers supply the base themselves; each knows its own default.
"""

from __future__ import annotations

from typing import Any


def probe_models(api_key: str, api_base: str, *, transport: Any = None) -> dict[str, Any]:
    """Return ``{ok, status, model_ids, error}`` for one key against one base.

    The ``/v1`` de-dup matters: some bases already end in ``/v1``, so appending
    ``/v1/models`` blindly would 404 and read as a bad key. ``transport`` is
    injectable for tests.
    """
    import httpx

    base = api_base.rstrip("/")
    url = base + "/models" if "/v1" in base else base + "/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    kwargs: dict[str, Any] = {"timeout": 10}
    if transport is not None:
        kwargs["transport"] = transport
    try:
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": "network_error", "model_ids": None, "error": str(exc)}
    if resp.status_code != 200:
        return {"ok": False, "status": f"http_{resp.status_code}", "model_ids": None, "error": resp.text[:200]}
    ids: list[str] = []
    try:
        for item in resp.json().get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(item["id"])
    except Exception:
        pass
    return {"ok": True, "status": "ok", "model_ids": ids, "error": None}
