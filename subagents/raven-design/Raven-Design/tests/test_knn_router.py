"""KNNModelRouter: argmax(reward - lambda*cost) over neighbours; graceful fallback."""

from __future__ import annotations

import json

import numpy as np
import pytest

from raven.config.schema import ModelEndpoint, RoutingConfig
from raven.routing.knn_router import KNNModelRouter

# large has higher reward but higher cost across all training tasks.
ENTRIES = [
    {
        "task_name": "a",
        "embedding": [1.0, 0.0],
        "rewards": {"small": 30, "large": 60},
        "costs": {"small": 1, "large": 10},
    },
    {
        "task_name": "b",
        "embedding": [0.0, 1.0],
        "rewards": {"small": 30, "large": 60},
        "costs": {"small": 1, "large": 10},
    },
    {
        "task_name": "c",
        "embedding": [1.0, 1.0],
        "rewards": {"small": 30, "large": 60},
        "costs": {"small": 1, "large": 10},
    },
]


def _write_memory(tmp_path, entries=ENTRIES):
    p = tmp_path / "mem.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return str(p)


def _cfg(
    memory_path,
    k=5,
    lam=0.0,
    models=("small", "large"),
    min_similarity=0.0,
    min_similar_neighbors=1,
    min_memory_size=1,
    min_margin=0.0,
):
    return RoutingConfig(
        enabled=True,
        backend="knn",
        k=k,
        lambda_cost=lam,
        embedding_endpoint="http://x/embed",
        memory_path=memory_path,
        models=[ModelEndpoint(model=m, api_base=f"http://{m}/v1") for m in models],
        min_similarity=min_similarity,
        min_similar_neighbors=min_similar_neighbors,
        min_memory_size=min_memory_size,
        min_margin=min_margin,
    )


# All neighbours point the same way; a query orthogonal to them has low cosine.
FAR_ENTRIES = [
    {
        "task_name": "a",
        "embedding": [1.0, 0.0, 0.0],
        "rewards": {"small": 30, "large": 60},
        "costs": {"small": 1, "large": 10},
    },
    {
        "task_name": "b",
        "embedding": [1.0, 0.0, 0.0],
        "rewards": {"small": 30, "large": 60},
        "costs": {"small": 1, "large": 10},
    },
]


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


@pytest.mark.asyncio
async def test_off_distribution_stays_on_default(tmp_path, monkeypatch):
    # Query orthogonal to every neighbour -> zero similar neighbours -> default.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path, FAR_ENTRIES), min_similarity=0.5, min_similar_neighbors=1))
    monkeypatch.setattr(r, "_embed", _const_embed([0.0, 1.0, 0.0]))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_too_few_similar_neighbors_stays_on_default(tmp_path, monkeypatch):
    # Only 2 of 3 neighbours clear cosine 0.6, but a switch requires >= 3.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), min_similarity=0.6, min_similar_neighbors=3))
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_memory_too_small_stays_on_default(tmp_path, monkeypatch):
    # 3 memory entries, but routing requires >= 10.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), min_memory_size=10))
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_small_margin_stays_on_default(tmp_path, monkeypatch):
    # large beats the default (small) by 30, but a switch requires margin >= 40.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path), min_margin=40.0), default_model="small")
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_already_on_default_no_switch(tmp_path, monkeypatch):
    # The best pick IS the default model -> no switch needed.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path)), default_model="large")
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    assert await r.select_model_chain("x") == (None, [])


@pytest.mark.asyncio
async def test_switches_when_all_gates_pass(tmp_path, monkeypatch):
    r = KNNModelRouter(
        _cfg(_write_memory(tmp_path), min_similarity=0.5, min_similar_neighbors=2, min_margin=10.0),
        default_model="small",
    )
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    primary, fallbacks = await r.select_model_chain("x")
    assert primary == "large"
    assert fallbacks == ["small"]


# Text-based memory (no stored embedding): the router embeds each entry's
# ``text`` at load via the configured endpoint.
TEXT_ENTRIES = [
    {"task_name": "a", "text": "alpha", "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
    {"task_name": "b", "text": "beta", "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
    {"task_name": "c", "text": "gamma", "rewards": {"small": 30, "large": 60}, "costs": {"small": 1, "large": 10}},
]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(text_to_vec):
    def _open(req, timeout=None):
        text = json.loads(req.data.decode())["texts"][0]
        return _FakeResp(json.dumps({"embeddings": [text_to_vec(text)]}).encode())

    return _open


@pytest.mark.asyncio
async def test_text_memory_embeds_at_load(tmp_path, monkeypatch):
    from raven.routing import knn_router as knn_mod

    vecs = {"alpha": [1.0, 0.0], "beta": [0.0, 1.0], "gamma": [1.0, 1.0]}
    monkeypatch.setattr(knn_mod.urllib.request, "urlopen", _fake_urlopen(lambda t: vecs[t]))

    r = KNNModelRouter(_cfg(_write_memory(tmp_path, TEXT_ENTRIES)))
    assert r._embeddings.shape == (3, 2)  # embedded from text at load

    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0]))
    primary, fallbacks = await r.select_model_chain("do a task")
    assert primary == "large"
    assert fallbacks == ["small"]


@pytest.mark.asyncio
async def test_routing_error_falls_back_to_default(tmp_path, monkeypatch):
    # A wrong-dimension query embedding makes the matmul raise; routing must
    # degrade to (None, []) rather than propagate the error into the turn.
    r = KNNModelRouter(_cfg(_write_memory(tmp_path)))  # memory vectors are 2-d
    monkeypatch.setattr(r, "_embed", _const_embed([1.0, 0.0, 0.0]))  # 3-d query
    assert await r.select_model_chain("x") == (None, [])
