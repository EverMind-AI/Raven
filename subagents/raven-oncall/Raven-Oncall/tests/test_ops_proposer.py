"""Tests for proposers (grid and LLM-driven), using a fake completion."""

from __future__ import annotations

from raven.ops import GridProposer, LLMProposer, config_key, make_openai_completer, make_raven_completer


async def test_grid_proposes_once_then_stops() -> None:
    grid = [{"k1": 0.8, "b": 0.3}, {"k1": 1.6, "b": 0.6}]
    proposer = GridProposer(grid)

    first = await proposer.propose([], 0)
    assert [t.trial_id for t in first] == [config_key(c) for c in grid]
    assert await proposer.propose([{"config": grid[0], "score": 0.3}], 1) == []


def _completer(text: str):
    async def complete(prompt: str) -> str:
        complete.prompt = prompt
        return text

    return complete


async def test_llm_seed_on_round_zero() -> None:
    seed = [{"k1": 1.0, "b": 0.5}]
    proposer = LLMProposer(_completer("[]"), objective="tune bm25", seed=seed)
    batch = await proposer.propose([], 0)
    assert [t.trial_id for t in batch] == [config_key(seed[0])]


async def test_llm_parses_filters_tried_and_caps_batch() -> None:
    text = 'sure: [{"k1": 1.0, "b": 0.5}, {"k1": 1.6, "b": 0.4}, {"k1": 1.6, "b": 0.4}, {"k1": 2.0, "b": 0.6}]'
    proposer = LLMProposer(_completer(text), objective="tune bm25", seed=[], batch_size=2, max_rounds=4)
    history = [{"config": {"k1": 1.0, "b": 0.5}, "score": 0.3}]

    batch = await proposer.propose(history, 1)

    ids = [t.trial_id for t in batch]
    assert config_key({"k1": 1.0, "b": 0.5}) not in ids  # already tried, filtered
    assert len(ids) == 2  # deduped + capped at batch_size
    assert ids[0] == config_key({"k1": 1.6, "b": 0.4})


async def test_llm_stops_at_max_rounds() -> None:
    proposer = LLMProposer(_completer("[{\"k1\": 9}]"), objective="x", seed=[], max_rounds=2)
    assert await proposer.propose([], 2) == []


async def test_raven_completer_adapts_provider_chat() -> None:
    class FakeResp:
        content = '[{"k1": 1.6, "b": 0.4}]'

    class FakeProvider:
        async def chat(self, messages, **kw):
            self.messages = messages
            return FakeResp()

    provider = FakeProvider()
    complete = make_raven_completer(provider, model="ep-x")
    out = await complete("propose configs")

    assert out == '[{"k1": 1.6, "b": 0.4}]'
    assert provider.messages == [{"role": "user", "content": "propose configs"}]


async def test_openai_completer_bypasses_proxy_and_disables_thinking(monkeypatch) -> None:
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '[{"k1": 1.5}]'}}]}

    class _Client:
        def __init__(self, **kw) -> None:
            captured["client_kw"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, json=None, headers=None):
            captured.update(url=url, payload=json)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    complete = make_openai_completer("https://x/v1", "qwen3.6-27B")
    out = await complete("propose")

    assert out == '[{"k1": 1.5}]'
    assert captured["client_kw"].get("trust_env") is False
    assert captured["payload"]["model"] == "qwen3.6-27B"
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["url"].endswith("/chat/completions")
