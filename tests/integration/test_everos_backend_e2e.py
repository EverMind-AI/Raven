"""L3 — raven.plugin.memory.everos backend <-> everos end-to-end (real LLM, embedded).

Drives the real :class:`EverosBackend` in embedded mode: ``store`` writes
turns into everos and ``recall`` reads both tracks back as ``Memory``
objects. This proves the plugin wiring + user_id/agent_id routing +
Memory mapping against a live everos, complementing L2 (which validates
extraction quality via direct service calls + is_final flush).

Boundary note: ``backend.store`` has no flush, so extraction here is
triggered **by volume** — the session fixture sets a tight
``HARD_MSG_LIMIT`` and we send several turns. The authoritative
skill-quality assertions live in L2; here the must-pass checks are the
deterministic ones (call success, Memory shape, owner-track routing,
dual-track isolation). The "a skill was recalled" check is best-effort
and xfails rather than flakily failing if clustering hasn't converged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.memory_engine import Memory
from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos.backend import EverosBackend, _RealEverosAdapter

pytestmark = pytest.mark.real_llm


def _backend(tmp_path: Path, *, agent_id: str) -> EverosBackend:
    be = EverosBackend(PluginContext(
        config={"mode": "embedded", "agent_id": agent_id},
        services=ServiceLocator(workspace=tmp_path),
    ))
    # Embedded mode must resolve the real adapter now that everos is
    # installed — if it degraded to no-op the e2e would be meaningless.
    assert isinstance(be._adapter, _RealEverosAdapter), (
        "embedded backend did not bind the real everos adapter"
    )
    return be


def _stamp_user(messages: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
    """Set user-message sender_id to the user identity so the stored
    owner matches what recall(user_id=<user_id>) queries (the backend
    keeps incoming user sender_id; assistant/tool get agent_id)."""
    out = []
    for m in messages:
        if m.get("role") == "user":
            out.append({**m, "sender_id": user_id})
        else:
            out.append(m)
    return out


async def _store_repeated(
    be: EverosBackend, session: str, messages: list[dict[str, Any]], times: int,
) -> None:
    """Send messages across several store() calls to cross the boundary."""
    for i in range(times):
        await be.store(f"{session}-{i}", messages)


async def test_user_track_recall_through_backend(
    everos_env: Any, ids: Any, corpus: dict[str, Any], tmp_path: Path,
    pipeline_drain: Any,
) -> None:
    be = _backend(tmp_path, agent_id=ids.agent_id)
    await be.start()
    try:
        facts = _stamp_user(corpus["user_facts"]["messages"], ids.user_id)
        await _store_repeated(be, ids.session, facts, times=2)
        await pipeline_drain()

        hits = await be.recall(
            "user preferences", user_id=ids.user_id, top_k=5,
        )
        assert isinstance(hits, list)
        for h in hits:
            assert isinstance(h, Memory)
            assert h.metadata.get("owner_type") == "user"
            assert h.metadata.get("type") in ("episode", "profile")
    finally:
        await be.stop()


async def test_agent_skill_recall_through_backend(
    everos_env: Any, ids: Any, corpus: dict[str, Any], tmp_path: Path,
    pipeline_drain: Any,
) -> None:
    be = _backend(tmp_path, agent_id=ids.agent_id)
    await be.start()
    try:
        demo = corpus["skill_demo"]
        for j, sess in enumerate(demo["sessions"]):
            msgs = _stamp_user(sess["messages"], ids.user_id)
            await _store_repeated(be, f"{ids.session}-{j}", msgs, times=1)
        await pipeline_drain()

        hits = await be.recall(demo["query"], agent_id=ids.agent_id, top_k=10)
        assert isinstance(hits, list)

        # Deterministic: whatever came back is shaped + routed correctly.
        for h in hits:
            assert isinstance(h, Memory)
            assert h.metadata.get("owner_type") == "agent"
            assert h.metadata.get("type") in ("skill", "case")

        # Best-effort: a clustered skill surfaced through the backend.
        skills = [h for h in hits if h.metadata.get("type") == "skill"]
        if not skills:
            pytest.xfail(
                "no skill clustered via boundary-by-volume; see "
                "test_everos_extraction_real_llm for the authoritative "
                "skill-quality test (uses is_final flush)"
            )
        blob = " ".join(h.text for h in skills).lower()
        assert any(k.lower() in blob for k in demo["expect_keywords"])
    finally:
        await be.stop()


async def test_dual_track_isolation(
    everos_env: Any, ids: Any, corpus: dict[str, Any], tmp_path: Path,
    pipeline_drain: Any,
) -> None:
    """A user-track query must never surface agent skills/cases."""
    be = _backend(tmp_path, agent_id=ids.agent_id)
    await be.start()
    try:
        await _store_repeated(
            be, ids.session,
            _stamp_user(corpus["user_facts"]["messages"], ids.user_id), times=2,
        )
        await pipeline_drain()
        user_hits = await be.recall(
            "rotate deploy key", user_id=ids.user_id, top_k=10,
        )
        assert all(h.metadata.get("owner_type") == "user" for h in user_hits)

        # Calling recall with neither track id returns empty (not a crash).
        assert await be.recall("anything", top_k=5) == []
    finally:
        await be.stop()
