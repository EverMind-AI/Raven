"""Real-LLM end-to-end check of the knn routing backend through AgentLoop.

Builds a tiny two-cluster memory, wires a real ``KNNModelRouter`` +
``PerModelProvider`` over two OpenRouter models, and drives
``AgentLoop._process_message`` so routing fires where raven triggers it
(the model-routing block in the loop). Asserts the simple task routes to the
cheap model, the hard task to the strong one, and the routed model actually
answers.

Independent of the everos ``real_llm`` fixtures: it hits OpenRouter + an
embedding endpoint directly, and self-skips unless both are available.
Configurable via env: OPENROUTER_API_KEY (required), RAVEN_EMBED_URL,
RAVEN_ROUTE_CHEAP_MODEL, RAVEN_ROUTE_STRONG_MODEL.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

EMBED_URL = os.environ.get("RAVEN_EMBED_URL", "http://localhost:9100/embed")
CHEAP = os.environ.get("RAVEN_ROUTE_CHEAP_MODEL", "qwen/qwen-2.5-7b-instruct")
STRONG = os.environ.get("RAVEN_ROUTE_STRONG_MODEL", "meta-llama/llama-3.3-70b-instruct")
OR_BASE = "https://openrouter.ai/api/v1"


def _embed_one(text: str) -> list[float]:
    req = urllib.request.Request(
        EMBED_URL, data=json.dumps({"texts": [text]}).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["embeddings"][0]


def _embed_reachable() -> bool:
    try:
        return isinstance(_embed_one("ping"), list)
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(not os.environ.get("OPENROUTER_API_KEY"), reason="OPENROUTER_API_KEY not set"),
    pytest.mark.skipif(not _embed_reachable(), reason=f"embedding endpoint {EMBED_URL} unreachable"),
]

# Two clusters: simple tasks both models solve (cheap far cheaper) and hard
# tasks only the strong model solves. Illustrative rewards/costs.
_TASKS = [
    ("simple: answer a short factual question", {CHEAP: 0.95, STRONG: 0.97}, {CHEAP: 0.0002, STRONG: 0.006}),
    ("simple: a one-word fact or basic arithmetic", {CHEAP: 0.95, STRONG: 0.97}, {CHEAP: 0.0002, STRONG: 0.006}),
    (
        "hard: multi-step algorithm design with correct code",
        {CHEAP: 0.40, STRONG: 0.95},
        {CHEAP: 0.0003, STRONG: 0.008},
    ),
    (
        "hard: subtle mathematical proof or tricky reasoning",
        {CHEAP: 0.40, STRONG: 0.95},
        {CHEAP: 0.0003, STRONG: 0.008},
    ),
]


def _build_memory(path: Path) -> None:
    # Text-based memory (the shipped format): the router embeds each entry's
    # text at load, so this exercises the load-time embedding path end-to-end.
    mem = [{"task_name": t[0], "text": t[0], "rewards": t[1], "costs": t[2]} for t in _TASKS]
    path.write_text(json.dumps(mem))


@pytest.mark.asyncio
async def test_knn_routing_end_to_end(tmp_path):
    from raven.agent.loop import AgentLoop
    from raven.config.schema import ModelEndpoint, RoutingConfig
    from raven.providers.custom_provider import CustomProvider
    from raven.providers.per_model_provider import PerModelProvider
    from raven.routing.knn_router import KNNModelRouter
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    key = os.environ["OPENROUTER_API_KEY"]
    mem_path = tmp_path / "mem.json"
    _build_memory(mem_path)

    cfg = RoutingConfig(
        enabled=True,
        backend="knn",
        k=2,
        lambda_cost=10.0,
        embedding_endpoint=EMBED_URL,
        memory_path=str(mem_path),
        models=[
            ModelEndpoint(model=CHEAP, api_base=OR_BASE, api_key=key),
            ModelEndpoint(model=STRONG, api_base=OR_BASE, api_key=key),
        ],
        # Tiny hand-built memory: relax the production safety gates.
        min_similarity=0.0,
        min_similar_neighbors=1,
        min_memory_size=1,
    )
    router = KNNModelRouter(cfg, default_model=CHEAP)
    provider = PerModelProvider(cfg.models, fallback=CustomProvider(api_key=key, api_base=OR_BASE, default_model=CHEAP))

    simple = "What is the capital of Japan? Answer in one word."
    hard = "Write a Python function using matrix exponentiation to compute the nth Fibonacci number in O(log n)."

    # Cost-aware routing with the cheap model as the agent default: the simple
    # task stays on the default (returns None), the hard task switches to strong.
    assert (await router.select_model_chain(simple)) == (None, [])
    assert (await router.select_model_chain(hard))[0] == STRONG

    # Full raven turn: routing fires inside _process_message and the routed
    # model returns a real answer (sandbox / MCP bring-up stubbed out).
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path,
        router=router,
        model=CHEAP,
        max_iterations=6,
        interactive=False,
    )

    async def _noop():
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop

    def _req(text: str) -> TurnRequest:
        return TurnRequest(
            origin=Origin.USER,
            source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
            text=text,
        )

    out = await loop._process_message(_req(simple))
    assert out is not None
    content, _media = out
    assert content and content.strip()
