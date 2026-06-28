"""L2/L3 — self-evolution e2e: channel weather conversations distil a reusable
agent skill, retrievable through the production recall path.

Several channel-style weather dialogs — each a genuine two-step tool
trajectory (``curl wttr.in`` summary, then a verify query) sharing one
verify-before-report procedure — are flushed through everos with
``is_final=True`` so the boundary fires and extraction runs
deterministically (the backend's ``store`` has no flush; that flaky
boundary-by-volume path is covered elsewhere). EverOS extracts a case per
session, clusters them, and distils a single weather skill. We then
assert that:

1. a clustered ``agent_skill`` exists, well-formed, weather-related
   (the authoritative extraction check, mirroring
   ``test_everos_extraction_real_llm``); and
2. the distilled skill is retrievable through the production
   :meth:`EverosBackend.recall` path — the same call ``EverosSkillSource``
   makes to fill the ``# Skills`` context section — returned as ``type==skill``.

Assertions are structural + keyword, never exact-string, because LLM
extraction is non-deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.memory_engine import Memory
from raven.plugin import PluginContext, ServiceLocator
from raven.plugin.memory.everos.backend import EverosBackend, _RealEverosAdapter
from tests.integration.conftest import as_everos_payload

pytestmark = pytest.mark.real_llm

# Descriptive recall query — bare "weather" matches the source cases more
# strongly than the distilled skill; a procedure-style query surfaces the
# skill (and the real agent's query is the user's full, descriptive message).
_RECALL_QUERY = "retrieve and verify the current weather conditions for a city"
_EXPECT_KEYWORDS = ("weather", "wttr", "verify")
# Eight sessions of the same procedure give clustering a strong enough
# signal to reliably mint a weather skill (4 was flaky — the LLM sometimes
# kept the cases as separate size-1 clusters without distilling a skill).
_CITIES = [
    ("Tokyo", "+18C", "Partly cloudy"),
    ("London", "+12C", "Light rain"),
    ("Paris", "+15C", "Sunny"),
    ("Berlin", "+10C", "Overcast"),
    ("Madrid", "+22C", "Clear"),
    ("Oslo", "+3C", "Snow"),
    ("Cairo", "+30C", "Sunny"),
    ("Lima", "+19C", "Cloudy"),
]


def _backend(tmp_path: Path, *, agent_id: str) -> EverosBackend:
    be = EverosBackend(PluginContext(
        config={"mode": "embedded", "agent_id": agent_id},
        services=ServiceLocator(workspace=tmp_path),
    ))
    assert isinstance(be._adapter, _RealEverosAdapter), (
        "embedded backend did not bind the real everos adapter"
    )
    return be


def _weather_session(city: str, temp: str, cond: str, user_id: str) -> list[dict]:
    """A channel weather dialog with a real two-step tool round-trip.

    The same verify-before-report shape across cities is what lets EverOS
    cluster the per-session cases into one reusable weather skill.
    """
    return [
        {"role": "user", "sender_id": user_id,
         "content": f"What's the weather in {city} right now?"},
        {"role": "assistant",
         "content": f"Checking {city} — I'll query wttr.in for a one-line summary.",
         "tool_calls": [{"id": f"{city}-1", "type": "function", "function": {
             "name": "exec",
             "arguments": f'{{"command": "curl -s wttr.in/{city}?format=3"}}'}}]},
        {"role": "tool", "tool_call_id": f"{city}-1", "content": f"{city}: {cond} {temp}"},
        {"role": "assistant",
         "content": "Re-querying condition+temp+wind to verify before reporting.",
         "tool_calls": [{"id": f"{city}-2", "type": "function", "function": {
             "name": "exec",
             "arguments": f'{{"command": "curl -s wttr.in/{city}?format=%C+%t+%w"}}'}}]},
        {"role": "tool", "tool_call_id": f"{city}-2", "content": f"{cond} {temp} 11km/h"},
        {"role": "assistant",
         "content": (
             f"It's {cond.lower()} and {temp} in {city}. Procedure: query "
             "wttr.in/<city>?format=3 for a quick summary, then re-query "
             "wttr.in/<city>?format=%C+%t+%w to confirm condition, temperature "
             "and wind before reporting. Always verify with the second query."
         )},
    ]


async def _memorize_weather(user_id: str, agent_id: str) -> None:
    """Flush each weather session through everos so extraction runs now."""
    from everos.service.memorize import memorize

    for i, (city, temp, cond) in enumerate(_CITIES):
        await memorize(
            as_everos_payload(
                f"weather-{city.lower()}-{i}",
                _weather_session(city, temp, cond, user_id),
                user_id=user_id, agent_id=agent_id,
            ),
            is_final=True,
        )


async def test_weather_skill_evolves_and_is_recallable(
    everos_env: Any, ids: Any, tmp_path: Path, pipeline_drain: Any,
) -> None:
    # 1) Drive the channel weather conversations (boundary flushed each turn).
    await _memorize_weather(ids.user_id, ids.agent_id)
    await pipeline_drain()

    # 2) Authoritative extraction check: a clustered weather skill exists.
    from everos.memory.search.dto import SearchRequest
    from everos.service.search import search

    data = (await search(SearchRequest(
        agent_id=ids.agent_id, query=_RECALL_QUERY, top_k=10))).data
    assert data.agent_skills, (
        "expected >=1 clustered agent_skill from the weather demonstrations; "
        "none extracted (prompt/model regression, or clustering changed)"
    )
    skill = max(data.agent_skills, key=lambda s: s.score)
    assert 0.0 <= skill.confidence <= 1.0
    assert 0.0 <= skill.maturity_score <= 1.0
    assert skill.source_case_ids, "skill should reference the cases it clustered from"
    blob = f"{skill.name}\n{skill.description}\n{skill.content}".lower()
    assert any(k in blob for k in _EXPECT_KEYWORDS), (
        f"extracted skill not weather-related; got name={skill.name!r} "
        f"desc={skill.description[:160]!r}"
    )

    # 3) Retrieve through the production recall path (what EverosSkillSource
    #    uses to fill ``# Skills``): the distilled skill must come back as
    #    ``type == skill``, not only its source cases.
    be = _backend(tmp_path, agent_id=ids.agent_id)
    await be.start()
    try:
        hits = await be.recall(_RECALL_QUERY, agent_id=ids.agent_id, top_k=10)
    finally:
        await be.stop()

    assert hits, "production recall returned nothing from the agent track"
    for h in hits:
        assert isinstance(h, Memory)
        assert h.metadata.get("owner_type") == "agent"
        assert h.metadata.get("type") in ("skill", "case")

    skill_hits = [h for h in hits if h.metadata.get("type") == "skill"]
    assert skill_hits, (
        "the distilled weather skill was not surfaced by backend.recall "
        f"(only cases came back: {[h.text[:50] for h in hits]})"
    )
    recalled = " ".join(h.text for h in skill_hits).lower()
    assert any(k in recalled for k in _EXPECT_KEYWORDS), (
        f"recalled skill not weather-related; got:\n{recalled[:400]}"
    )
