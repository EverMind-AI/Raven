"""The cloud memory plugin against the live EverOS Cloud.

Everything else in the plugin's suite runs against a fake written from the
service's documentation; these are the only calls that reach the service, and
the only evidence that the documentation and the service agree (design spec
C14-C18). They need ``EVEROS_CLOUD_API_KEY`` and are skipped without it; the
two-minute extraction poll is additionally held behind ``EVEROS_CLOUD_SLOW=1``.

Every test uses a fresh ``(session_id, user_id)`` pair so runs never read each
other's memory, and prints the service's raw replies so a failure carries the
service's own words.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest

from raven.plugins import PluginContext, ServiceLocator
from raven_everos_cloud.backend import DEFAULT_BASE_URL, EverosCloudBackend

KEY = os.environ.get("EVEROS_CLOUD_API_KEY", "")
pytestmark = pytest.mark.skipif(
    not KEY, reason="EVEROS_CLOUD_API_KEY is not set; the live EverOS Cloud tests need a key"
)

_ROUTE = f"{DEFAULT_BASE_URL}/api/v2/memory"


def _ids() -> tuple[str, str]:
    token = uuid.uuid4().hex[:12]
    return f"ecm-live-{token}", f"ecm-live-user-{token}"


def _backend(tmp_path: Path, user_id: str, key: str = KEY) -> EverosCloudBackend:
    ctx = PluginContext(
        config={"api_key": key},
        services=ServiceLocator(workspace=tmp_path, user_id=user_id, agent_id=f"{user_id}-agent"),
        logger=logging.getLogger("live"),
    )
    return EverosCloudBackend(ctx)


def _say(label: str, payload: object) -> None:
    print(f"\n[{label}] {payload}")


async def test_add_flush_search_roundtrip(tmp_path: Path) -> None:
    """C14: the request bodies the plugin sends are accepted end to end."""
    session, user = _ids()
    b = _backend(tmp_path, user)
    try:
        landed = await b.store(
            session,
            [
                {"role": "user", "content": "I take my coffee black, no sugar, and I live in Lisbon."},
                {"role": "assistant", "content": "Noted: black coffee, no sugar, Lisbon."},
            ],
            metadata={"flush": True},
        )
        assert landed is True
        hits = []
        for _ in range(8):
            await asyncio.sleep(2.0)
            hits = await b.recall("coffee", user_id=user, top_k=5)
            if hits:
                break
        _say("search", [h.text for h in hits])
        assert hits, "nothing recalled within the settle window"
    finally:
        await b.stop()


async def test_get_by_session(tmp_path: Path) -> None:
    """C14: ``/get`` filtered by session answers the rows an add produced."""
    session, user = _ids()
    b = _backend(tmp_path, user)
    try:
        assert await b.store(
            session, [{"role": "user", "content": "My bike is a red Brompton."}], metadata={"flush": True}
        )
        rows = []
        for _ in range(8):
            await asyncio.sleep(2.0)
            rows = await b.recall_session(session, user_id=user)
            if rows:
                break
        _say("get", [r.metadata for r in rows])
        assert rows and all(r.metadata.get("session_id") == session for r in rows)
    finally:
        await b.stop()


async def test_flush_with_nothing_pending_is_no_extraction() -> None:
    """C15: ``/flush`` exists on the cloud and is harmless with nothing pending."""
    session, _ = _ids()
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_ROUTE}/flush", json={"session_id": session}, headers={"Authorization": f"Bearer {KEY}"}
        )
    _say("flush", (r.status_code, r.text))
    assert r.status_code == 200 and r.json()["data"]["status"] == "no_extraction"


async def test_wrong_key_is_401(tmp_path: Path) -> None:
    """C16: a deliberately wrong key is refused as 401, which the health probe words as 'key rejected'."""
    _, user = _ids()
    b = _backend(tmp_path, user, key="ecm-wrong-key")
    try:
        health = await b.health()
    finally:
        await b.stop()
    _say("health", health)
    assert health.ready is False and "rejected (401)" in (health.checks[0].hint or "")


@pytest.mark.skipif(
    not os.environ.get("EVEROS_CLOUD_SLOW"), reason="costs two minutes; set EVEROS_CLOUD_SLOW=1 to run it by hand"
)
async def test_add_without_flush_is_searchable_within_two_minutes(tmp_path: Path) -> None:
    """C17: the cloud extracts on its own after an add that nobody flushed."""
    session, user = _ids()
    b = _backend(tmp_path, user)
    try:
        assert await b.store(session, [{"role": "user", "content": "I play the cello on Thursdays."}]) is True
        deadline = time.monotonic() + 120.0
        hits = []
        while time.monotonic() < deadline:
            await asyncio.sleep(10.0)
            hits = await b.recall("cello", user_id=user, top_k=5)
            if hits:
                break
        _say("search", [h.text for h in hits])
        assert hits, "nothing extracted within two minutes without a flush"
    finally:
        await b.stop()


async def test_get_page_one_with_and_without_key(tmp_path: Path) -> None:
    """C18: the probe ``/get page_size=1`` is accepted with a key and refused without one."""
    _, user = _ids()
    body = {"memory_type": "episode", "user_id": user, "page_size": 1}
    async with httpx.AsyncClient(timeout=30.0) as client:
        ok = await client.post(f"{_ROUTE}/get", json=body, headers={"Authorization": f"Bearer {KEY}"})
        bare = await client.post(f"{_ROUTE}/get", json=body)
    _say("get", (ok.status_code, ok.text[:200], bare.status_code))
    assert 200 <= ok.status_code < 300 and bare.status_code == 401
