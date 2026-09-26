"""Prompt resources, proactive guidance and memory projection reach native worker execution."""

from copy import deepcopy
from functools import partial

import pytest

from experimental.curator.harness import Task
from experimental.curator.raven_adapter.worker import Worker, WorkerError
from raven.contracts.llm_provider import LLMResponse
from tests.integration.test_harness_curator_e2e import baseline as baseline
from tests.integration.test_harness_curator_e2e import plan_for, replay_provider

ACTION = """from pydantic import BaseModel
from experimental.curator.harness.prompts import Prompt
from experimental.curator.harness.strategies import ActionStrategy

class Situation(BaseModel):
    question: str
class Failure(BaseModel):
    reason: str
class Decision(BaseModel):
    accepted: bool
class Inputs(BaseModel):
    goal: str
    question: str

GUIDANCE = Prompt.from_file(__file__, "prompts/guide.md", Inputs)

class Action(ActionStrategy[Situation, Failure, Decision]):
    def __init__(self, state, task, infer):
        self.state, self.task, self.infer = state, task, infer
    async def guide(self, proposal: Situation) -> str | None:
        self.state["guidance_calls"] = self.state.get("guidance_calls", 0) + 1
        return GUIDANCE.render(Inputs(goal=self.task.text, question=proposal.question))
    async def assess(self, proposal: Situation) -> Decision:
        return Decision(accepted=True)
    async def recover(self, failure: Failure) -> Decision:
        return Decision(accepted=False)

def create(state, task, *, infer):
    return Action(state, task, infer)

def situation(step):
    return Situation(question=step.question)
"""
MEMORY = """from experimental.curator.harness.strategies import MemoryStrategy
from experimental.curator.harness.strategies.memory import ContextRequest, ContextView

class Memory(MemoryStrategy[str, str, str, bool]):
    async def recall(self, query: str) -> str:
        return ""
    async def retain(self, record: str) -> bool:
        return False
    async def compose(self, request: ContextRequest) -> ContextView:
        return ContextView(messages=[*request.messages[:-1],
            {"role": "assistant", "content": "noise " * request.budget}, request.messages[-1]])
    async def compact(self, request: ContextRequest) -> ContextView:
        required = [request.messages[i] for i in request.required]
        return ContextView(messages=[*required[:-1],
            {"role": "assistant", "content": "MEMORY_COMPACTED"}, required[-1]])

def create(state, task):
    return Memory()
"""


def artifact():
    return {
        "values": {
            "action.strategy": {"factory": "action:create", "guidance": "action:situation"},
            "prompt.resources": ["action:GUIDANCE"],
        },
        "files": {"action.py": ACTION, "prompts/guide.md": "GUIDANCE_A: $goal / $question"},
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prompt_guidance_reaches_model_and_revision_preserves_state(baseline, tmp_path):
    baseline.task = Task(id="prompt-task", text="Use evidence")
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        proposed = artifact()
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(plan_for(*proposed["values"]), proposed)
        assert (await worker.check(candidate)).passed
        await worker.install(candidate)
        first = await worker.run("First question")
        assert not first.errors
        assert any(
            "GUIDANCE_A: Use evidence / First question" in str(row)
            for row in first.records
            if row["kind"] == "provider.request"
        )
        facts = (await worker.inspect()).facts
        assert facts["prompts"]["action:GUIDANCE"]["input_schema"]["required"] == ["goal", "question"]
        assert facts["action"]["sessions"]["curator:task"]["guidance_calls"] == 1
        change = {
            "values": {"prompt.resources": ["action:GUIDANCE"]},
            "files": {"prompts/guide.md": "GUIDANCE_B: $goal / $question"},
        }
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for("prompt.resources"), change))
        second = await worker.run("Second question")
        assert not second.errors
        assert any(
            "GUIDANCE_B: Use evidence / Second question" in str(row)
            for row in second.records
            if row["kind"] == "provider.request"
        )
        assert (await worker.inspect()).facts["action"]["sessions"]["curator:task"]["guidance_calls"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_compacts_native_context_and_rejects_lost_protected_messages(baseline, tmp_path):
    baseline.task = Task(text="Keep the current goal")
    proposed = {
        "values": {"memory.strategy": {"factory": "memory:create", "composition": True}},
        "files": {"memory.py": MEMORY},
    }
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for(*proposed["values"]), proposed))
        result = await worker.run("Keep this exact current question")
        assert not result.errors
        assert any(row["kind"] == "memory.call" and row["operation"] == "compact" for row in result.records)
        assert any(
            "MEMORY_COMPACTED" in str(row) and "Keep this exact current question" in str(row)
            for row in result.records
            if row["kind"] == "provider.request"
        )
        bad = deepcopy(proposed)
        bad["files"]["memory.py"] = bad["files"]["memory.py"].replace(
            "required = [request.messages[i] for i in request.required]", "required = [request.messages[-1]]"
        )
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for("memory.strategy"), bad))
        with pytest.raises(WorkerError, match="protected") as failed:
            await worker.run("Preserve the user message")
        assert not any(row["kind"] == "provider.request" for row in failed.value.records)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_strategy_inference_is_distinct_from_worker_model_decision(baseline, tmp_path):
    baseline.task = Task(text="Use an explicit inference dependency")
    proposed = artifact()
    proposed["files"]["action.py"] = proposed["files"]["action.py"].replace(
        "return GUIDANCE.render(Inputs(goal=self.task.text, question=proposal.question))",
        'return await self.infer([{"role": "user", "content": GUIDANCE.render(Inputs(goal=self.task.text, question=proposal.question))}])',
    )
    provider = partial(
        replay_provider, responses=[LLMResponse(content="INFERRED_GUIDANCE"), LLMResponse(content="Final answer")]
    )
    async with Worker(baseline, tmp_path / "runtime", provider_factory=provider, timeout=30) as worker:
        inspection = await worker.inspect()
        await worker.install(inspection.declaration.accept(plan_for(*proposed["values"]), proposed))
        result = await worker.run("Work")
        assert not result.errors
        assert any(row["kind"] == "strategy.inference" for row in result.records)
        requests = [row for row in result.records if row["kind"] == "provider.request"]
        assert len(requests) == 2 and "INFERRED_GUIDANCE" in str(requests[-1])
