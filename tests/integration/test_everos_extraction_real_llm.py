"""L2 — everos extraction quality (real LLM, direct service calls).

This layer bypasses the raven.plugin.memory.everos backend and drives
``everos.service.memorize`` directly with ``is_final=True`` so a
boundary is forced and extraction runs deterministically (the backend's
``store`` has no flush path — that's covered as boundary-by-volume in
the L3 e2e test). It validates that:

- user-track ingestion yields episodes / profiles whose content matches
  the seeded facts;
- repeated agent demonstrations cluster into an ``agent_skill`` whose
  name / description / content match the demonstrated procedure, with
  well-formed confidence / maturity / source_case_ids.

Assertions are intentionally **structural + semantic-keyword**, never
exact-string, because LLM extraction is non-deterministic. Tighten the
keyword sets in ``data/everos_skill_corpus.json`` alongside any prompt
changes; promote to an LLM-judge check (``@pytest.mark.llm_judge``) when
stricter fidelity is needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import as_everos_payload

pytestmark = pytest.mark.real_llm


async def _memorize_session(
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    user_id: str,
    agent_id: str,
) -> Any:
    """Flush one session through everos so extraction runs now."""
    from everos.service.memorize import memorize

    return await memorize(
        as_everos_payload(session_id, messages, user_id=user_id, agent_id=agent_id),
        is_final=True,
    )


async def _search(
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    query: str,
    top_k: int = 10,
) -> Any:
    from everos.memory.search.dto import SearchRequest
    from everos.service.search import search

    resp = await search(SearchRequest(
        user_id=user_id, agent_id=agent_id, query=query, top_k=top_k,
    ))
    return resp.data


# ---------------------------------------------------------------------------
# User track — episodes / profiles
# ---------------------------------------------------------------------------


async def test_user_memory_extracted_and_matches(
    everos_env: Any, ids: Any, corpus: dict[str, Any], pipeline_drain: Any,
) -> None:
    facts = corpus["user_facts"]
    result = await _memorize_session(
        ids.session, facts["messages"], user_id=ids.user_id, agent_id=ids.agent_id,
    )
    assert result.status in ("extracted", "accumulated")
    await pipeline_drain()

    data = await _search(user_id=ids.user_id, query="what are the user's preferences")

    # Something landed on the user track.
    assert data.episodes or data.profiles, "expected user episodes or profiles"

    blob = " ".join(
        [getattr(e, "summary", "") + " " + getattr(e, "episode", "")
         for e in data.episodes]
        + [str(p.profile_data) for p in data.profiles]
    ).lower()
    matched = [k for k in facts["expect_keywords"] if k.lower() in blob]
    assert matched, (
        f"recalled user memory matched none of {facts['expect_keywords']}; "
        f"got: {blob[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Agent track — clustered skills
# ---------------------------------------------------------------------------


async def test_agent_skill_extracted_and_matches(
    everos_env: Any, ids: Any, corpus: dict[str, Any], pipeline_drain: Any,
) -> None:
    demo = corpus["skill_demo"]

    # Repeat the same procedure across sessions so case clustering fires.
    for i, sess in enumerate(demo["sessions"]):
        await _memorize_session(
            f"{ids.session}-{i}", sess["messages"],
            user_id=ids.user_id, agent_id=ids.agent_id,
        )
    await pipeline_drain()

    data = await _search(agent_id=ids.agent_id, query=demo["query"], top_k=10)

    # 1) Structural — a clustered skill exists and is well-formed.
    assert data.agent_skills, (
        "expected >=1 clustered agent_skill; none extracted. If everos "
        "extraction is correct but slow to cluster, raise the number of "
        "demonstration sessions in the corpus."
    )
    skill = max(data.agent_skills, key=lambda s: s.score)
    assert 0.0 <= skill.confidence <= 1.0
    assert 0.0 <= skill.maturity_score <= 1.0
    assert skill.score > 0.0
    assert skill.source_case_ids, "skill should reference the cases it clustered from"

    # 2) Semantic — name/description/content relate to the demonstrated task.
    blob = f"{skill.name}\n{skill.description}\n{skill.content}".lower()
    matched = [k for k in demo["expect_keywords"] if k.lower() in blob]
    assert matched, (
        f"extracted skill matched none of {demo['expect_keywords']}; "
        f"got name={skill.name!r} desc={skill.description[:160]!r}"
    )


async def test_agent_cases_recorded(
    everos_env: Any, ids: Any, corpus: dict[str, Any], pipeline_drain: Any,
) -> None:
    """Cases are the raw material skills cluster from — assert they land."""
    demo = corpus["skill_demo"]
    for i, sess in enumerate(demo["sessions"]):
        await _memorize_session(
            f"{ids.session}-case-{i}", sess["messages"],
            user_id=ids.user_id, agent_id=ids.agent_id,
        )
    await pipeline_drain()

    data = await _search(agent_id=ids.agent_id, query=demo["query"], top_k=10)
    assert data.agent_cases or data.agent_skills, (
        "expected agent cases (or already-clustered skills) on the agent track"
    )
    for case in data.agent_cases:
        assert case.task_intent
        assert 0.0 <= case.quality_score <= 1.0
