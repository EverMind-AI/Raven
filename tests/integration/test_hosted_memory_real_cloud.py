"""The three hosted backends against their real services.

Runs by hand, never in CI: ``uv run pytest tests/integration/test_hosted_memory_real_cloud.py``
with ``MEM0_API_KEY`` / ``ZEP_API_KEY`` / ``MEMOS_API_KEY`` exported. A missing
key fails the run and says which -- a skipped integration test reads as green
to anyone counting.

Each run uses a fresh user id and deletes everything it wrote on the way out.
Ingestion is asynchronous on all three services, so recall is polled with a
ceiling; the ceilings are experience, not contract, and a run that needs more
should record why before raising them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

import pytest

from raven.contracts.memory import Memory
from raven.memory_engine.http_backend import Call, HttpMemoryBackend
from raven.plugins import PluginContext, ServiceLocator
from raven_mem0.backend import Mem0Backend
from raven_memos.backend import MemosBackend
from raven_zep.backend import ZepBackend

BACKENDS: dict[str, type[HttpMemoryBackend]] = {"mem0": Mem0Backend, "zep": ZepBackend, "memos": MemosBackend}
KEYS = {name: os.environ.get(cls.ENV_KEY, "") for name, cls in BACKENDS.items()}

INGEST_CEILING_S = 120.0
FORGET_CEILING_S = 60.0
POLL_S = 5.0


@pytest.fixture(autouse=True)
def _keys_present_and_out_of_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = [BACKENDS[n].ENV_KEY for n, k in KEYS.items() if not k]
    if missing:
        pytest.fail(f"real-cloud run needs {', '.join(missing)} exported; nothing was skipped")
    # The tests hand each backend its key through the slice, so a wrong key
    # can be tested without the environment overriding it.
    for cls in BACKENDS.values():
        monkeypatch.delenv(cls.ENV_KEY, raising=False)


def _backend(name: str, tmp_path: Path, *, api_key: str | None = None, user_id: str) -> HttpMemoryBackend:
    ctx = PluginContext(
        config={"api_key": KEYS[name] if api_key is None else api_key},
        services=ServiceLocator(workspace=tmp_path, user_id=user_id, agent_id="raven-it-agent"),
        logger=logging.getLogger("raven.plugins.hosted-memory"),
    )
    return BACKENDS[name](ctx)


async def _purge(name: str, backend: HttpMemoryBackend, user_id: str) -> None:
    if name == "mem0":
        await backend._send(Call("DELETE", "/v1/memories/", params={"user_id": user_id}), timeout=30)
    elif name == "zep":
        await backend._send(Call("DELETE", f"/api/v2/users/{user_id}"), timeout=30)
    else:
        await backend._send(Call("POST", "/delete/memory", json={"user_id": user_id}), timeout=30)


async def _poll(predicate, *, ceiling: float) -> tuple[bool, float]:
    started = time.monotonic()
    while True:
        if await predicate():
            return True, time.monotonic() - started
        if time.monotonic() - started > ceiling:
            return False, time.monotonic() - started
        await asyncio.sleep(POLL_S)


@pytest.fixture
def user_id() -> str:
    return f"raven-it-{uuid.uuid4().hex[:8]}"


@pytest.fixture(params=sorted(BACKENDS))
async def live(request, tmp_path: Path, user_id: str):
    name = request.param
    backend = _backend(name, tmp_path, user_id=user_id)
    await backend.start()
    try:
        yield name, backend
    finally:
        try:
            await _purge(name, backend, user_id)
        finally:
            await backend.stop()


# ── I-1 ───────────────────────────────────────────────────────────────


async def test_health_is_ready(live) -> None:
    name, backend = live
    health = await backend.health()
    assert health is not None and health.ready is True, f"{name}: {health}"
    assert [c.status for c in health.checks] == ["ok"], f"{name}: {health.checks}"


# ── I-2 / I-3 / I-4 / I-6 in one ingestion ────────────────────────────


async def test_store_recall_session_delete_and_top_k(live, user_id: str) -> None:
    name, backend = live
    token = f"quartz-{uuid.uuid4().hex[:6]}"
    session = f"sess-{uuid.uuid4().hex[:6]}"
    messages = [
        {"role": "user", "content": f"My project codename is {token}. Please remember it."},
        {"role": "assistant", "content": f"Got it, your project codename is {token}."},
        {"role": "user", "content": "Also, I drink oolong tea every morning."},
        {"role": "assistant", "content": "Oolong every morning, noted."},
    ]
    assert await backend.store(session, messages) is True, f"{name}: store not accepted"

    hits: list[Memory] = []

    async def _recalled() -> bool:
        nonlocal hits
        hits = await backend.recall(token, user_id=user_id, top_k=5)
        return any(token.lower() in h.text.lower() for h in hits)

    found, took = await _poll(_recalled, ceiling=INGEST_CEILING_S)
    assert found, f"{name}: {token!r} not recalled within {took:.0f}s; last hits={[h.text for h in hits]}"
    hit = next(h for h in hits if token.lower() in h.text.lower())
    assert hit.metadata.get("id"), f"{name}: hit carries no native id: {hit.metadata}"
    assert 0.0 <= hit.score <= 1.0

    # I-6: the service holds at least two facts now; one is what we asked for.
    one = await backend.recall("tea", user_id=user_id, top_k=1)
    assert len(one) <= 1, f"{name}: top_k=1 returned {len(one)}"

    # I-4: the conversation reads back by its session id.
    session_rows = await backend.recall_session(session, user_id=user_id)
    assert session_rows, f"{name}: recall_session({session!r}) is empty"
    assert any(token.lower() in r.text.lower() for r in session_rows), [r.text for r in session_rows]

    # I-3: delete by the id recall handed out, then it stops coming back.
    assert await backend.delete(hit.metadata["id"], kind=hit.metadata.get("kind")) is True, f"{name}: delete refused"

    async def _forgotten() -> bool:
        now = await backend.recall(token, user_id=user_id, top_k=5)
        return all(h.metadata.get("id") != hit.metadata["id"] for h in now)

    gone, took = await _poll(_forgotten, ceiling=FORGET_CEILING_S)
    assert gone, f"{name}: memory {hit.metadata['id']} still recalled {took:.0f}s after delete"


# ── I-5 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(BACKENDS))
async def test_wrong_key_reads_as_missing_and_recall_is_quiet(name: str, tmp_path: Path, user_id: str) -> None:
    backend = _backend(name, tmp_path, api_key=f"bad-{KEYS[name][:4]}-0000000000", user_id=user_id)
    try:
        health = await backend.health()
        assert health.ready is False and health.checks[0].status == "missing", f"{name}: {health}"
        assert KEYS[name] not in (health.checks[0].hint or "")
        assert await backend.recall("anything", user_id=user_id, top_k=3) == []
    finally:
        await backend.stop()
