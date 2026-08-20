"""Unit tests for RoutineAggregator (MS6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.proactive_engine.sentinel.predictor.routine_aggregator import RoutineAggregator
from raven.proactive_engine.sentinel.predictor.routine_store import RoutineStore
from raven.proactive_engine.sentinel.types import Routine

_NOW_MS = 1_700_000_000_000


def _routine(
    rid: str = "dow1-h09-meeting",
    *,
    pattern: str = "Tuesday 09:00-12:00 — meeting",
    keywords: tuple[str, ...] = ("meeting", "standup"),
    day_of_week: int | None = 1,
    time_slot: tuple[int, int] | None = (9, 12),
    occurrence_count: int = 4,
    weight: float = 4.0,
    description: str | None = None,
) -> Routine:
    return Routine(
        id=rid,
        pattern=pattern,
        keywords=list(keywords),
        day_of_week=day_of_week,
        time_slot=time_slot,
        status="candidate",
        occurrence_count=occurrence_count,
        weight=weight,
        description=description,
    )


class _StubProvider:
    def __init__(self, items: list[dict] | None, *, has_tool_calls: bool = True, raw_args: str | None = None) -> None:
        if has_tool_calls:
            args = raw_args if raw_args is not None else json.dumps({"routines": items or []})

            class _Call:
                arguments = args

            class _Resp:
                pass

            self._resp = _Resp()
            self._resp.has_tool_calls = True
            self._resp.tool_calls = [_Call()]
        else:

            class _Resp:
                pass

            self._resp = _Resp()
            self._resp.has_tool_calls = False
            self._resp.tool_calls = []
        self.calls: list[dict] = []

    async def chat_with_retry(self, *, messages, tools, model, tool_choice):
        self.calls.append({"messages": messages})
        return self._resp


@pytest.fixture
def routine_store(tmp_path: Path) -> RoutineStore:
    store = RoutineStore(tmp_path / "routines.json")
    store.merge(
        [
            _routine("dow1-h09-meeting"),
            _routine("dow3-h09-sync", pattern="Wednesday 09:00-12:00 — sync", keywords=("sync", "team"), day_of_week=2),
        ],
        now_ms=_NOW_MS,
    )
    return store


# ── happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_applies_descriptions(routine_store):
    provider = _StubProvider(
        [
            {
                "id": "dow1-h09-meeting",
                "description": "Tuesday morning engineering standup",
                "semantic_group": "morning_standup",
            },
            {
                "id": "dow3-h09-sync",
                "description": "Wednesday morning team sync",
                "semantic_group": "morning_standup",
            },
        ]
    )
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )

    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 2

    r1 = routine_store.get("dow1-h09-meeting")
    assert r1.description == "Tuesday morning engineering standup"
    assert r1.semantic_group == "morning_standup"

    r2 = routine_store.get("dow3-h09-sync")
    assert r2.description == "Wednesday morning team sync"
    assert r2.semantic_group == "morning_standup"


@pytest.mark.asyncio
async def test_aggregate_skips_already_described_routines(routine_store):
    # Pre-populate one description; only the un-described one should be sent
    routine_store.upsert_description(
        "dow1-h09-meeting",
        description="Tuesday standup (manually set)",
        semantic_group="manual",
    )

    provider = _StubProvider(
        [
            {
                "id": "dow3-h09-sync",
                "description": "Wednesday team sync",
                "semantic_group": "morning_sync",
            },
        ]
    )
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )

    candidates = routine_store.candidates()
    n = await aggregator.aggregate(candidates)
    assert n == 1

    # The pre-set description should be preserved
    r1 = routine_store.get("dow1-h09-meeting")
    assert r1.description == "Tuesday standup (manually set)"

    # Verify that only the pending one was sent to the LLM (compact check
    # via the system/user prompt content)
    assert len(provider.calls) == 1
    user_msg = provider.calls[0]["messages"][1]["content"]
    assert "dow3-h09-sync" in user_msg
    assert "dow1-h09-meeting" not in user_msg


@pytest.mark.asyncio
async def test_aggregate_skips_unknown_ids(routine_store):
    """LLM hallucination defense: descriptions for ids we never asked
    about are dropped."""
    provider = _StubProvider(
        [
            {"id": "dow1-h09-meeting", "description": "Tuesday standup", "semantic_group": "x"},
            {"id": "made-up-id", "description": "fake routine", "semantic_group": "y"},
        ]
    )
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )

    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 1  # only the real one
    assert routine_store.get("made-up-id") is None


@pytest.mark.asyncio
async def test_aggregate_skips_empty_descriptions(routine_store):
    provider = _StubProvider(
        [
            {"id": "dow1-h09-meeting", "description": "  ", "semantic_group": "x"},  # empty → drop
            {"id": "dow3-h09-sync", "description": "valid one"},
        ]
    )
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )
    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 1
    r1 = routine_store.get("dow1-h09-meeting")
    assert r1.description is None or r1.description.strip() == ""


@pytest.mark.asyncio
async def test_aggregate_no_pending_routines_returns_zero(routine_store):
    # Pre-describe both
    routine_store.upsert_description("dow1-h09-meeting", description="x", semantic_group="g")
    routine_store.upsert_description("dow3-h09-sync", description="y", semantic_group="g")
    provider = _StubProvider([])
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )
    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 0
    # LLM never called
    assert len(provider.calls) == 0


@pytest.mark.asyncio
async def test_aggregate_no_tool_call_returns_zero(routine_store):
    provider = _StubProvider(items=None, has_tool_calls=False)
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )
    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 0


@pytest.mark.asyncio
async def test_aggregate_malformed_json_returns_zero(routine_store):
    provider = _StubProvider(items=None, has_tool_calls=True, raw_args="not json")
    aggregator = RoutineAggregator(
        provider=provider,
        model="x",
        routine_store=routine_store,
    )
    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 0


@pytest.mark.asyncio
async def test_aggregate_provider_exception_does_not_propagate(routine_store):
    class _Boom:
        async def chat_with_retry(self, **kw):
            raise RuntimeError("kaboom")

    aggregator = RoutineAggregator(
        provider=_Boom(),
        model="x",
        routine_store=routine_store,
    )
    n = await aggregator.aggregate(routine_store.candidates())
    assert n == 0
