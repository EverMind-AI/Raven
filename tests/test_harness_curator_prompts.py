"""Prompt contracts and optional strategy operations fail explicitly at their real boundaries."""

import pytest
from pydantic import BaseModel

from experimental.curator.harness import Artifact
from experimental.curator.harness.prompts import Prompt
from experimental.curator.raven_adapter.inference import Inference
from experimental.curator.raven_adapter.materialize import write_package
from experimental.curator.raven_adapter.observe import Recorder
from experimental.curator.raven_adapter.prompts import bind_prompts
from raven.contracts.llm_provider import LLMResponse


class Inputs(BaseModel):
    goal: str
    evidence: list[str]


def prompt(tmp_path, text="Goal: ${goal}\nEvidence: ${evidence}\nCost: $$5"):
    (tmp_path / "guide.md").write_text(text)
    return Prompt.from_file(str(tmp_path / "defs.py"), "guide.md", Inputs)


def test_prompt_validates_and_substitutes_without_expanding_input(tmp_path):
    template = prompt(tmp_path)
    result = template.render(Inputs(goal="literal ${evidence}", evidence=["a", "b"]))
    assert result == 'Goal: literal ${evidence}\nEvidence: ["a", "b"]\nCost: $5'
    assert template.describe()["input_schema"]["required"] == ["goal", "evidence"]
    for value in ({"goal": "x"}, {"goal": 1, "evidence": []}, {"goal": "x", "evidence": [], "extra": 1}):
        with pytest.raises(ValueError):
            template.render(value)
    corrupted = Inputs(goal="x", evidence=[])
    corrupted.__dict__["goal"] = 3
    with pytest.raises((ValueError, TypeError)):
        template.render(corrupted)


@pytest.mark.parametrize("text", ["$unknown", "${broken", "$5"])
def test_prompt_rejects_invalid_template_contract(tmp_path, text):
    with pytest.raises(ValueError):
        prompt(tmp_path, text)


def test_prompt_admission_uses_real_object_and_candidate_content(tmp_path):
    artifact = Artifact(
        values={"prompt.resources": ["defs:GUIDE"]},
        files={
            "defs.py": "from pydantic import BaseModel\nfrom experimental.curator.harness.prompts import Prompt\n"
            'class Inputs(BaseModel):\n    goal: str\nGUIDE = Prompt.from_file(__file__, "guide.md", Inputs)\n',
            "guide.md": "Goal: $goal",
        },
    )
    package = write_package(tmp_path, artifact)
    facts = bind_prompts(artifact, package)
    assert facts["defs:GUIDE"]["file"] == "guide.md"
    changed = Artifact(values=artifact.values, files={**artifact.files, "guide.md": "changed"})
    with pytest.raises(ValueError, match="differs"):
        bind_prompts(changed, package)
    invalid = Artifact(values={"prompt.resources": ["defs:Inputs"]}, files=artifact.files)
    with pytest.raises(TypeError, match="not a Prompt"):
        bind_prompts(invalid, package)


@pytest.mark.asyncio
async def test_inference_budget_is_shared_within_turn_and_failures_are_evidence(tmp_path):
    class Provider:
        async def chat_with_retry(self, **kwargs):
            return LLMResponse(content="result")

    recorder = Recorder(tmp_path / "records.jsonl")
    infer = Inference(Provider(), recorder, max_calls=1)
    assert await infer([{"role": "user", "content": "Evaluate"}]) == "result"
    with pytest.raises(RuntimeError, match="budget"):
        await infer([{"role": "user", "content": "Again"}])
    recorder.turn_id = "next"
    assert await infer([{"role": "user", "content": "Evaluate"}]) == "result"
    recorder.turn_id = "invalid"
    with pytest.raises(ValueError):
        await infer([{"role": "tool", "content": "forged"}])
