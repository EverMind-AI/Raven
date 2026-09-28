"""Semantic strategy boundaries, retained state and native translations."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError

from experimental.curator.harness import Artifact, Task
from experimental.curator.raven_adapter.action.contracts import ActionBinding
from experimental.curator.raven_adapter.action.runtime import BoundAction
from experimental.curator.raven_adapter.capability.contracts import CapabilityBinding
from experimental.curator.raven_adapter.capability.runtime import BoundCapability
from experimental.curator.raven_adapter.materialize import write_package
from experimental.curator.raven_adapter.memory.contracts import MemoryBinding
from experimental.curator.raven_adapter.memory.runtime import BoundMemory
from experimental.curator.raven_adapter.observe import Recorder
from raven.contracts.participant import StepView

FIXTURES = Path(__file__).parent / "fixtures/harness_curator"
ROLES = {
    "memory": (MemoryBinding, BoundMemory),
    "capability": (CapabilityBinding, BoundCapability),
    "action": (ActionBinding, BoundAction),
}


def files():
    return {
        name: (FIXTURES / name).read_text()
        for name in ("task_memory.py", "task_capability.py", "task_action.py", "strategy_bindings.py")
    }


def values():
    return {
        "memory.strategy": {
            "factory": "task_memory:create",
            "query": "strategy_bindings:query",
            "context": "strategy_bindings:context",
            "retain": "strategy_bindings:retain",
        },
        "capability.strategy": {
            "factory": "task_capability:create",
            "need": "strategy_bindings:need",
            "expose": "strategy_bindings:expose",
            "context": "strategy_bindings:capability_context",
        },
        "action.strategy": {
            "factory": "task_action:create",
            "proposal": "strategy_bindings:proposal",
            "decision": "strategy_bindings:decision",
            "failure": "strategy_bindings:failure",
            "reply": "strategy_bindings:reply",
        },
    }


def make(tmp_path, role, contents=None, task=None):
    package = write_package(tmp_path / "package", Artifact(values={}, files=contents or files()))
    binding, implementation = ROLES[role]
    return implementation(
        binding.model_validate(values()[f"{role}.strategy"]),
        task or Task(id="task", text="Find evidence"),
        tmp_path / f"{role}.json",
        package,
        Recorder(tmp_path / "records.jsonl"),
    )


def step(**kwargs):
    base = StepView(
        session_key="session",
        iteration=1,
        response=None,
        transcript=(),
        history=(),
        turn_base=0,
        question="Find evidence",
        rollbacks=0,
        mode=None,
        mode_overlay=None,
        phase="iteration",
    )
    return replace(base, **kwargs)


@pytest.mark.asyncio
async def test_memory_retrieval_deduplication_and_restore(tmp_path):
    owner = make(tmp_path, "memory")
    await owner.prepare()
    evidence = {"call_id": "call", "value": "cobalt"}
    assert (await owner.retain(evidence)).stored
    assert not (await owner.retain(evidence)).stored
    before = owner.path.read_bytes()
    assert (await owner.recall({"text": "cobalt"})).facts == ["cobalt"]
    assert (await owner.recall({"text": "missing"})).facts == []
    assert owner.path.read_bytes() == before
    restored = make(tmp_path, "memory")
    await restored.prepare()
    assert restored.path.read_bytes() == before
    assert restored.hook().factory is not None


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.asyncio
async def test_checkpoints_reject_other_task_identity(tmp_path, role):
    owner = make(tmp_path, role)
    await owner.prepare()
    with pytest.raises(ValueError, match="another task"):
        make(tmp_path, role, task=Task(id="other", text="Find evidence"))


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.asyncio
async def test_translation_result_validation_records_inputs_before_any_consumer_runs(tmp_path, role):
    class Detail(BaseModel):
        count: int

    class Result(BaseModel):
        detail: Detail

    owner = make(tmp_path, role)
    await owner.prepare()
    before = owner.path.read_bytes()
    supplied = {"count": 3}

    def translate(value):
        result = Result(detail=Detail(count=value["count"]))
        result.detail = {"count": value["count"]}
        return result

    with pytest.raises(PydanticSerializationError):
        owner.translate("context", translate, supplied, output=Result)
    error = next(row for row in owner.recorder.rows if row["kind"] == f"{role}.error")
    assert error["arguments"] == [supplied]
    assert error["state"] == owner.state
    assert owner.path.read_bytes() == before
    assert owner.translate("context", lambda value: Result(detail=value), supplied, output=Result).detail.count == 3


@pytest.mark.asyncio
async def test_memory_read_mutation_and_invalid_return_restore_owned_state(tmp_path):
    contents = files()
    contents["task_memory.py"] = contents["task_memory.py"].replace(
        "return Context(facts=", 'self.state["bad"] = True\n        return Context(facts='
    )
    owner = make(tmp_path, "memory", contents)
    await owner.prepare()
    before = owner.path.read_bytes()
    instance = owner.strategy
    with pytest.raises(ValueError, match="changed retained state"):
        await owner.recall({"text": ""})
    assert "bad" not in owner.state and owner.path.read_bytes() == before
    assert owner.strategy is instance
    contents = files()
    contents["task_memory.py"] = contents["task_memory.py"].replace(
        "return Receipt(stored=True)", 'return {"wrong": True}'
    )
    owner = make(tmp_path, "memory", contents)
    with pytest.raises(ValueError):
        await owner.retain({"call_id": "new", "value": "wrong"})
    assert owner.state == {"facts": {}} and owner.path.read_bytes() == before


@pytest.mark.asyncio
async def test_capability_resources_and_selection_do_not_fabricate_authority(tmp_path):
    owner = make(tmp_path, "capability")
    await owner.prepare()
    assert await owner.resources.tools[0].execute() == "FACT:cobalt"
    assert "evidence/SKILL.md" in owner.resources.skills
    participant = owner.hook().factory()
    offered = [owner.resources.tools[0].to_schema()]
    result = await participant.select_tools(offered, step())
    assert result == offered and result is not offered
    rendered = await participant.system_addendum(step())
    assert "CAPABILITY_USAGE:evidence_probe" in rendered["text"]
    assert owner.state["selections"] == 1
    await participant.system_addendum(step(tools=tuple(offered)))
    assert owner.state["selections"] == 2
    contents = files()
    contents["strategy_bindings.py"] = contents["strategy_bindings.py"].replace(
        "return selection.names", 'return ["invented"]'
    )
    bad = make(tmp_path, "capability", contents)
    with pytest.raises(ValueError, match="offered"):
        await bad.hook().factory().select_tools(offered, step())
    assert any(row["kind"] == "capability.error" for row in bad.recorder.rows)


@pytest.mark.parametrize(
    "replacement",
    [
        "return CapabilityResources(tools=(Probe(), Probe()))",
        'return CapabilityResources(skills={"../escape": "bad"})',
        'self.state["invalid"] = True\n        return CapabilityResources()',
    ],
)
def test_capability_rejects_invalid_resource_delivery(tmp_path, replacement):
    contents = files()
    start = contents["task_capability.py"].index("        return CapabilityResources(")
    end = contents["task_capability.py"].index("    async def select", start)
    contents["task_capability.py"] = (
        contents["task_capability.py"][:start]
        + "        "
        + replacement
        + "\n\n"
        + contents["task_capability.py"][end:]
    )
    with pytest.raises(ValueError):
        make(tmp_path, "capability", contents)


@pytest.mark.asyncio
async def test_action_decision_and_terminal_recovery_have_different_consumers(tmp_path):
    owner = make(tmp_path, "action")
    await owner.prepare()
    participant = owner.hook().factory()
    verdict = await participant.review(step(phase="after_iteration"))
    assert verdict["verdict"] == "resample" and verdict["inject"]
    reply = await participant.salvage(step(phase="answerless"))
    assert "incomplete" in reply
    assert owner.state == {"retries": 1, "recoveries": 1}
    restored = make(tmp_path, "action")
    assert restored.state == owner.state
    before = owner.path.read_bytes()
    with pytest.raises(ValueError):
        await owner.assess({"has_evidence": "false", "pending_tools": False})
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("role,method", [("memory", "recall"), ("capability", "select"), ("action", "assess")])
def test_missing_semantic_operations_are_not_silent_defaults(tmp_path, role, method):
    contents = files()
    name = f"task_{role}.py"
    contents[name] = contents[name].replace(f"async def {method}(", f"async def missing_{method}(")
    with pytest.raises(TypeError):
        make(tmp_path, role, contents)


def test_partial_translation_pairs_are_rejected():
    for cls, value in [
        (MemoryBinding, {"query": "bindings:query"}),
        (CapabilityBinding, {"need": "bindings:need"}),
        (ActionBinding, {"proposal": "bindings:proposal"}),
    ]:
        with pytest.raises(ValueError):
            cls(factory="strategy:create", **value)


def test_strategy_state_is_kept_per_session_while_memory_is_kept_for_the_task(tmp_path):
    from experimental.curator.raven_adapter.strategy import SESSION, Scopes

    task = Task(id="task", text="Serve travellers")
    action = Scopes("action", task, tmp_path / "action.json", lambda state: state, per_session=True)
    memory = Scopes("memory", task, tmp_path / "memory.json", lambda state: state, per_session=False)
    for key in ("traveller:a", "traveller:b"):
        token = SESSION.set(key)
        try:
            action.current().state["seen"] = key
            memory.current().state.setdefault("seen", []).append(key)
        finally:
            SESSION.reset(token)
    action.save()
    memory.save()
    assert action.sessions() == {"traveller:a": {"seen": "traveller:a"}, "traveller:b": {"seen": "traveller:b"}}
    assert memory.current().state == {"seen": ["traveller:a", "traveller:b"]} and memory.sessions() == {}
    reopened = Scopes("action", task, tmp_path / "action.json", lambda state: state, per_session=True)
    token = SESSION.set("traveller:b")
    try:
        assert reopened.current().state == {"seen": "traveller:b"} and reopened.current().resuming
    finally:
        SESSION.reset(token)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("inheritance", ["missing", "lookalike", "indirect"])
def test_strategy_factory_requires_the_public_protocol_in_its_inheritance_chain(tmp_path, role, inheritance):
    contents = files()
    path = f"task_{role}.py"
    name = role.title()
    protocol = f"{name}Strategy"
    line = next(line for line in contents[path].splitlines() if line.startswith(f"class {name}("))
    if inheritance == "missing":
        replacement = f"class {name}:"
    elif inheritance == "lookalike":
        replacement = f"class {protocol}:\n    pass\n\n\nclass {name}({protocol}):"
    else:
        base = line.replace(f"class {name}(", f"class Shared{name}(", 1)
        replacement = f"{base}\n    pass\n\n\nclass {name}(Shared{name}):"
    contents[path] = contents[path].replace(line, replacement)
    if inheritance == "indirect":
        assert make(tmp_path, role, contents).strategy is not None
    else:
        with pytest.raises(TypeError, match=f"explicitly inheriting {protocol}"):
            make(tmp_path, role, contents)
