"""KNNModelRouter: argmax(reward - lambda*cost) over neighbours; graceful fallback."""

from __future__ import annotations

import json

import numpy as np
import pytest

from raven.config.schema import ModelEndpoint, RoutingConfig
from raven.routing.knn_router import KNNModelRouter

# large has higher reward but higher cost across all training tasks.
ENTRIES = [
    {"task_name": "a", "embedding": [1.0, 0.0], "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
    {"task_name": "b", "embedding": [0.0, 1.0], "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
    {"task_name": "c", "embedding": [1.0, 1.0], "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
]


def _write_memory(tmp_path, entries=ENTRIES):
    p = tmp_path / "mem.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return str(p)


def _cfg(memory_path, k=3, lam=0.0, models=("small", "large")):
    return RoutingConfig(
        enabled=True,
        backend="knn",
        k=k,
        lambda_cost=lam,
        embedding_endpoint="http://x/embed",
        memory_path=memory_path,
        models=[ModelEndpoint(model=m, api_base=f"http://{m}/v1") for m in models],
    )


def _const_embed(vec):
    v = np.array(vec, dtype=np.float32)

    async def _e(prompt):
        return v / max(float(np.linalg.norm(v)), 1e-8)

    return _e


@pytest.mark.asyncio
async def test_routes_to_higher_reward(tmp_path, monkeypatch):
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), lam=0.0))
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    primary, fallbacks = await r.select_model_chain("do a task")
    assert primary == "large"
    assert fallbacks == ["small"]


@pytest.mark.asyncio
async def test_high_lambda_prefers_cheaper(tmp_path, monkeypatch):
    # large: 60 - 5*10 = 10 ; small: 30 - 5*1 = 25 -> small wins
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), lam=5.0))
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    primary, fallbacks = await r.select_model_chain("do a task")
    assert primary == "small"
    assert fallbacks == ["large"]


@pytest.mark.asyncio
async def test_missing_memory_returns_none(tmp_path):
    r = KNNModelRouter(_cfg("/nonexistent/mem.json"))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_embedding_failure_returns_none(tmp_path, monkeypatch):
    r = KNNModelRouter(_cfg(_write_memory(tmp_path)))

    async def _fail(prompt):
        return None

    monkeypatch.setattr(r, "_embed", _fail)
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_fewer_than_two_candidates_returns_none(tmp_path, monkeypatch):
    # config has a model not present in memory -> only one valid candidate.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), models=("small", "ghost")))
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    assert r._candidates == ["small"]
    assert await r.select_model_chain("x") == (None, [])
