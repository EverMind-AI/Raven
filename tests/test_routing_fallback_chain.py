"""``ModelRouter.select_model_chain`` exposes the selector's fallback list."""

from __future__ import annotations

import pytest

from raven.routing.router import ModelRouter
from raven.routing.types import ModelScore, SelectionResult


def _result(primary: str, fallbacks: list[str]) -> SelectionResult:
    def _score(m: str) -> ModelScore:
        return ModelScore(model=m, provider="p", task_score=1.0, cost_score=1.0, composite_score=1.0)

    return SelectionResult(
        primary=_score(primary),
        fallbacks=[_score(m) for m in fallbacks],
        category="tool_use",
        profile="balanced",
    )


@pytest.mark.asyncio
async def test_select_model_chain_returns_primary_and_fallbacks(monkeypatch):
    router = ModelRouter(api_key="test")

    async def fake_route(_prompt):
        return _result("a/primary", ["b/second", "c/third"])

    monkeypatch.setattr(router, "route", fake_route)
    primary, fallbacks = await router.select_model_chain("hi")
    assert primary == "a/primary"
    assert fallbacks == ["b/second", "c/third"]


@pytest.mark.asyncio
async def test_configured_fallback_model_appended_as_last_resort(monkeypatch):
    router = ModelRouter(api_key="test", fallback_model="z/default")

    async def fake_route(_prompt):
        return _result("a/primary", ["b/second"])

    monkeypatch.setattr(router, "route", fake_route)
    primary, fallbacks = await router.select_model_chain("hi")
    assert primary == "a/primary"
    assert fallbacks == ["b/second", "z/default"]


@pytest.mark.asyncio
async def test_configured_fallback_not_duplicated(monkeypatch):
    router = ModelRouter(api_key="test", fallback_model="b/second")

    async def fake_route(_prompt):
        return _result("a/primary", ["b/second"])

    monkeypatch.setattr(router, "route", fake_route)
    _primary, fallbacks = await router.select_model_chain("hi")
    assert fallbacks == ["b/second"]


@pytest.mark.asyncio
async def test_configured_fallback_skipped_when_equals_primary(monkeypatch):
    router = ModelRouter(api_key="test", fallback_model="a/primary")

    async def fake_route(_prompt):
        return _result("a/primary", [])

    monkeypatch.setattr(router, "route", fake_route)
    _primary, fallbacks = await router.select_model_chain("hi")
    assert fallbacks == []


@pytest.mark.asyncio
async def test_route_none_yields_empty_chain(monkeypatch):
    router = ModelRouter(api_key="test", fallback_model="z/default")

    async def fake_route(_prompt):
        return None

    monkeypatch.setattr(router, "route", fake_route)
    primary, fallbacks = await router.select_model_chain("hi")
    assert primary is None
    assert fallbacks == []
