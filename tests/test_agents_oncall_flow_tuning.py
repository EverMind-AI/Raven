"""Test the adaptive tuning loop end to end against the mock backend.

A scripted fake LLM proposes one new config per round (then stops); the loop
must run each proposed config, accumulate results, and return the best -- proving
propose -> run -> propose -> converge works and scoring stays in the backend.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.campaign import Campaign  # noqa: E402
from oncall_flow.ledger import Ledger  # noqa: E402
from oncall_flow.mock_backend import JobPlan, MockJobBackend  # noqa: E402
from oncall_flow.proposer import LLMProposer, config_key  # noqa: E402
from oncall_flow.tuning import tune  # noqa: E402

_SURFACE = {
    ("k1", 1.0, "b", 0.5): 0.30,
    ("k1", 1.5, "b", 0.5): 0.34,
    ("k1", 1.8, "b", 0.5): 0.33,
}


def _plans() -> dict[str, JobPlan]:
    plans = {}
    for (_, k1, _, b), score in _SURFACE.items():
        cfg = {"k1": k1, "b": b}
        plans[config_key(cfg)] = JobPlan(metrics={"ndcg": score}, output={"config": cfg})
    return plans


def _scripted_completer():
    responses = ['[{"k1": 1.5, "b": 0.5}]', '[{"k1": 1.8, "b": 0.5}]', "[]"]
    state = {"i": 0}

    async def complete(prompt: str) -> str:
        r = responses[state["i"]]
        state["i"] += 1
        return r

    return complete, state


async def test_adaptive_tune_converges_to_best(tmp_path: Path) -> None:
    backend = MockJobBackend(plans=_plans())
    campaign = Campaign("bm25", [], backend, Ledger(tmp_path / "l.json"), metric="ndcg", goal="max")
    complete, state = _scripted_completer()
    proposer = LLMProposer(complete, objective="tune bm25 k1/b for ndcg", seed=[{"k1": 1.0, "b": 0.5}], max_rounds=6)

    best = await tune(campaign, proposer, max_rounds=8)

    assert len(campaign.history()) == 3  # seed + 2 proposed configs all scored
    assert state["i"] == 3  # LLM consulted rounds 1,2,3 (last returns [])
    assert best is not None
    assert best.idem_key == config_key({"k1": 1.5, "b": 0.5})
    assert best.result.metrics["ndcg"] == 0.34
