"""PerModelProvider: routes calls to per-model endpoints, falls back for unknown models."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from raven.config.schema import ModelEndpoint
from raven.providers.per_model_provider import PerModelProvider


def _fallback():
    fb = MagicMock()
    fb.get_default_model.return_value = "fallback-model"
    return fb


def _provider():
    eps = [
        ModelEndpoint(model="small", api_base="http://a/v1", api_key="KA"),
        ModelEndpoint(model="large", api_base="http://b/v1", api_key="KB"),
    ]
    return PerModelProvider(eps, fallback=_fallback())


def test_pick_routes_by_model():
    p = _provider()
    assert p._pick("small") is p._by_model["small"]
    assert p._pick("large") is p._by_model["large"]


def test_pick_unknown_and_none_use_fallback():
    p = _provider()
    assert p._pick("nope") is p._fallback
    assert p._pick(None) is p._fallback


def test_get_default_model_is_first_configured():
    assert _provider().get_default_model() == "small"


def test_default_falls_back_when_no_models():
    p = PerModelProvider([], fallback=_fallback())
    assert p.get_default_model() == "fallback-model"


@pytest.mark.asyncio
async def test_chat_with_retry_dispatches_by_model():
    p = _provider()
    p._by_model["large"].chat_with_retry = AsyncMock(return_value="LARGE_RESP")
    p._by_model["small"].chat_with_retry = AsyncMock(return_value="SMALL_RESP")

    out = await p.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="large")

    assert out == "LARGE_RESP"
    p._by_model["large"].chat_with_retry.assert_awaited_once()
    assert p._by_model["large"].chat_with_retry.call_args.kwargs["model"] == "large"
    p._by_model["small"].chat_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_with_retry_unknown_model_uses_fallback():
    fb = _fallback()
    fb.chat_with_retry = AsyncMock(return_value="FB_RESP")
    p = PerModelProvider([ModelEndpoint(model="small", api_base="http://a/v1")], fallback=fb)

    out = await p.chat_with_retry(messages=[], model="other")

    assert out == "FB_RESP"
    fb.chat_with_retry.assert_awaited_once()
