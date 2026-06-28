"""HubSkillSource + SkillHubClient.search — discovery layer (P1).

Hermetic: a httpx MockTransport stands in for the Skill Hub, so no
network. Covers envelope unwrapping, RouterHit mapping, safety-score
filtering, and graceful empties. Body-hydrate moved to
SkillsSegmentBuilder (see test_skill_segment_builder.py); this layer
returns metadata-only RouterHits.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from raven.memory_engine.skill_forge import HubSkillSource
from raven.memory_engine.skill_forge.types import RouterHit
from raven.skill_hub import SkillHubClient
from raven.skill_hub.client import SkillHubError


def _envelope(result: dict[str, Any], *, error: str = "ok", status: int = 0) -> dict:
    return {"error": error, "requestId": "req-1", "status": status, "result": result}


def _item(id_: str, name: str, *, score=0.9, safety=None) -> dict:
    """A Hub catalog item in the dev/aws schema. ``score_safety`` is absent
    by default (the real catalog payload omits it — it's detail-only); pass
    ``safety=`` to exercise the optional safety guard."""
    it = {
        "id": id_, "skill_id": f"acme/{name.lower().replace(' ', '-')}",
        "name": name, "description": f"{name} does things.",
        "source": "acme", "category": "ops", "tags": ["x"],
        "quality_score": score, "body_tokens": 1000, "install_count": 10,
        "download_url": f"https://hub.test/openapi/v1/skills/{id_}/download",
    }
    if safety is not None:
        it["score_safety"] = safety
    return it


def _client(handler) -> SkillHubClient:
    transport = httpx.MockTransport(handler)
    return SkillHubClient(
        "https://hub.test", api_key="k",
        client=httpx.AsyncClient(transport=transport),
    )


# ---------------------------------------------------------------------------
# SkillHubClient.search
# ---------------------------------------------------------------------------


class TestClientSearch:
    async def test_search_unwraps_envelope_and_sends_headers(self) -> None:
        seen: dict[str, Any] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["path"] = req.url.path
            seen["q"] = req.url.params.get("q")
            seen["auth"] = req.headers.get("authorization")
            seen["reqid"] = req.headers.get("x-request-id")
            return httpx.Response(200, json=_envelope({"items": [_item("a", "Alpha")]}))

        c = _client(handler)
        items = await c.search("calendar", limit=5)
        assert seen["path"] == "/openapi/v1/skills"
        assert seen["q"] == "calendar"
        assert seen["auth"] == "Bearer k"
        assert seen["reqid"]                       # X-Request-ID present
        assert [it["name"] for it in items] == ["Alpha"]
        await c.aclose()

    async def test_non_ok_envelope_raises(self) -> None:
        def handler(req):
            return httpx.Response(200, json=_envelope({}, error="bad", status=7))

        c = _client(handler)
        with pytest.raises(SkillHubError):
            await c.search("q")
        await c.aclose()


# ---------------------------------------------------------------------------
# HubSkillSource.search → RouterHit
# ---------------------------------------------------------------------------


class TestHubSource:
    async def test_maps_items_to_router_hits(self) -> None:
        def handler(req):
            return httpx.Response(200, json=_envelope({"items": [
                _item("id1", "Calendar Scheduling", score=0.91),
                _item("id2", "Email Triage", score=0.80),
            ]}))

        src = HubSkillSource(_client(handler), weight=0.85)
        hits = await src.search("schedule", [], k=10)
        assert len(hits) == 2
        h = hits[0]
        assert isinstance(h, RouterHit)
        assert h.qualified_id == "hub/id1"         # UUID is the native id
        assert h.name == "Calendar Scheduling"
        assert h.content == ""                     # Tier 0: metadata only
        assert h.score == pytest.approx(0.91)      # from quality_score
        assert h.meta["source"] == "hub"
        assert h.meta["id"] == "id1"
        assert h.meta["skill_id"] == "acme/calendar-scheduling"
        assert h.meta["tags"] == ["x"]
        assert src.weight == 0.85

    async def test_filters_below_min_safety(self) -> None:
        def handler(req):
            return httpx.Response(200, json=_envelope({"items": [
                _item("ok", "Safe", safety=0.9),
                _item("no", "Risky", safety=0.4),
            ]}))

        src = HubSkillSource(_client(handler), min_safety=0.7)
        hits = await src.search("q", [], k=10)
        assert [h.name for h in hits] == ["Safe"]

    async def test_missing_id_or_name_skipped(self) -> None:
        def handler(req):
            return httpx.Response(200, json=_envelope({"items": [
                {"id": "x"},                                   # no name
                _item("good", "Good"),
            ]}))

        src = HubSkillSource(_client(handler))
        hits = await src.search("q", [], k=10)
        assert [h.name for h in hits] == ["Good"]

    async def test_all_hits_metadata_only(self) -> None:
        """HubSkillSource is Tier 0 / discovery — body hydrate now happens
        in SkillsSegmentBuilder's pre-gate stage instead. Verifies the
        retired ``prefetch_bodies`` knob isn't silently re-introducing body
        fetches at the source layer."""
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_envelope({"items": [
                _item("id1", "Top", score=0.95),
                _item("id2", "Low", score=0.3),
            ]}))

        src = HubSkillSource(_client(handler))
        hits = await src.search("q", [], k=10)
        assert all(h.content == "" for h in hits)
