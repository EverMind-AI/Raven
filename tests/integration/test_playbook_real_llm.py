"""Live-LLM regression for playbook generation, over ``cases.yaml``.

Lives here rather than under ``tests/`` because it dispatches to a real
provider: ``tests/integration`` is in ``norecursedirs``, so a default unit run
neither collects nor reports it. Run it explicitly:

    uv run pytest tests/integration/test_playbook_real_llm.py -v

Assertions check decision-level expectations (mode, role casting, faithful
step transcription, honest self-reporting), not exact output text — the
generator is a model call, so the only stable contract is the shape of its
decisions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml


def _config_key(provider: str) -> str:
    """A key from the user's own config, so a configured machine needs no env."""
    path = Path.home() / ".raven" / "config.json"
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get("providers", {}).get(provider) or {}
    except Exception:
        return ""
    return section.get("apiKey") or section.get("api_key") or ""


OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY") or _config_key("openrouter")

#: Pinned rather than taken from the machine's default model, for two reasons.
#: These assertions are about *generation decisions* (mode, role casting,
#: faithful transcription), which only compare across runs if the model is the
#: same one. And generation is a forced tool call -- a provider that ignores
#: ``tool_choice`` fails every case for a reason that has nothing to do with the
#: code under test, which is what a default-model run produced on a Codex
#: machine.
MODEL = "openrouter/anthropic/claude-fable-5"

pytestmark = [
    pytest.mark.real_llm,
    pytest.mark.skipif(not OPENROUTER_KEY, reason="no OpenRouter credential (env or ~/.raven)"),
]

_CASES = yaml.safe_load(
    (Path(__file__).parent.parent / "fixtures" / "playbook" / "cases.yaml").read_text(encoding="utf-8")
)


def _make_generator():
    from raven.agent.subagent.registry import AgentRegistry
    from raven.playbook import PlaybookGenerator, StaticInventory
    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(api_key=OPENROUTER_KEY, default_model=MODEL, provider_name="openrouter")
    # The agent table's own rows: the roster the generator casts nodes against has
    # to be the one the graph can then dispatch to. It used to be a private
    # four-name pool with no connection to either.
    registry = AgentRegistry()
    registry.apply([])
    return PlaybookGenerator(
        provider,
        skill_router=None,  # candidates not needed for decision-level checks
        agent_roster=registry.descriptions(),
        inventory=StaticInventory(mcp=[], tools=[]),
        model=MODEL,
    )


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
async def test_case(case):
    gen = _make_generator()
    result = await gen.generate(case["input"], skills=case.get("pinned_skills"))
    spec = result.spec
    expect = case["expect"]

    assert spec.mode == expect["mode"], f"mode: got {spec.mode}, want {expect['mode']}"

    if "agents_used" in expect:
        used = {n.agent for n in spec.nodes or []}
        assert set(expect["agents_used"]) <= used, f"agents: want {expect['agents_used']} within {used}"
    if "node_count" in expect:
        assert spec.nodes is not None and len(spec.nodes) == expect["node_count"]
    if expect.get("parallel_pair"):
        roots = [n for n in spec.nodes if not n.depends_on]
        assert len(roots) >= 2, "expected two independent scan nodes"
    if expect.get("prompts_required"):
        assert (spec.prompts or "").strip(), "prompt mode must ship assembly guidance"
    if "open_questions_min" in expect:
        questions = [n for n in result.notes if n.startswith("Open question:")]
        assert len(questions) >= expect["open_questions_min"], result.notes
    if "must_reference_skills" in expect:
        bound = {s for n in spec.nodes or [] for s in n.skills}
        joined_prompts = spec.prompts or ""
        assert all(sk in bound or sk in joined_prompts for sk in expect["must_reference_skills"])
