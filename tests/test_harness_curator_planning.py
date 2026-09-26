"""Planning contracts support distinct representations and explicit shared ownership."""

import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest
from jsonschema import Draft202012Validator

from experimental.curator.harness import Artifact
from experimental.curator.harness.declaration import parse_as, schema_for
from experimental.curator.harness.strategies import PlanningStrategy
from experimental.curator.raven_adapter.materialize import load_factory, write_package

SAMPLE = Path(__file__).parent / "fixtures/harness_curator/planning.py"


@pytest.fixture
def example(tmp_path):
    package = write_package(tmp_path, Artifact(values={}, files={"planning.py": SAMPLE.read_text()}))
    return lambda name: load_factory(f"planning:{name}", package)


@pytest.fixture(params=["ChecklistPlanning", "GraphPlanning"])
def planner(request, example):
    return example(request.param)()


def replacement(planner):
    view_type = get_type_hints(planner.view)["return"]
    return view_type(items=[]) if "items" in view_type.model_fields else view_type(nodes={})


def test_public_protocol_does_not_import_raven():
    code = (
        "import sys; from experimental.curator.harness.strategies import PlanningStrategy; "
        "assert not any(name == 'raven' or name.startswith('raven.') for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], cwd=SAMPLE.parents[3], check=True, capture_output=True, text=True)


def test_inherited_missing_operation_cannot_silently_return_none():
    class Incomplete(PlanningStrategy[str, str]):
        async def initialize(self, task: str) -> str:
            return task

        async def view(self) -> str:
            return "view"

    with pytest.raises(TypeError, match="abstract.*revise"):
        Incomplete()


@pytest.mark.asyncio
async def test_initialization_empty_state_and_task_identity_are_distinct(planner):
    with pytest.raises(RuntimeError, match="not initialized"):
        await planner.view()
    with pytest.raises(RuntimeError, match="not initialized"):
        await planner.revise(replacement(planner))
    first = await planner.initialize("Verify the change")
    assert (await planner.initialize("A later message")).model_dump() == first.model_dump()
    empty = replacement(planner)
    assert (await planner.revise(empty)).model_dump() == empty.model_dump()
    assert (await planner.initialize("Another message")).model_dump() == empty.model_dump()
    assert (await planner.revise(empty)).model_dump() == empty.model_dump()


@pytest.mark.asyncio
async def test_views_and_submitted_values_do_not_alias_owned_state(planner):
    first = await planner.initialize("Verify")
    expected = first.model_dump()
    if hasattr(first, "items"):
        first.items.clear()
    else:
        first.nodes.clear()
    assert (await planner.view()).model_dump() == expected
    submitted = get_type_hints(planner.view)["return"].model_validate(expected)
    returned = await planner.revise(submitted)
    if hasattr(submitted, "items"):
        submitted.items[0].done = True
        returned.items.clear()
    else:
        submitted.nodes["Verify"].done = True
        returned.nodes.clear()
    assert (await planner.view()).model_dump() == expected


@pytest.mark.asyncio
async def test_typed_request_schemas_and_runtime_rejections_preserve_state(planner, example):
    initial = await planner.initialize("Verify")
    change_type = get_type_hints(planner.revise)["change"]
    schema = schema_for(change_type)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    raw = {"step": "Verify", "succeeded": True}
    assert validator.is_valid(raw)
    result = await planner.revise(parse_as(change_type, raw, strict=True))
    assert result.model_dump() != initial.model_dump()
    expected = result.model_dump()
    for raw in ({"unexpected": True}, {"step": "Verify", "succeeded": True, "trusted": True}):
        assert not validator.is_valid(raw)
        with pytest.raises(ValueError):
            parse_as(change_type, raw, strict=True)
    with pytest.raises(ValueError, match="unknown step"):
        await planner.revise(example("StepResult")(step="missing", succeeded=True))
    with pytest.raises(TypeError, match="unsupported"):
        await planner.revise(None)
    assert (await planner.view()).model_dump() == expected


@pytest.mark.asyncio
async def test_graph_policy_validates_dependencies_before_committing(example):
    planner = example("GraphPlanning")()
    await planner.initialize("Verify")
    graph = example("GraphView")
    result_type = example("StepResult")
    initial = await planner.revise(graph(nodes={"build": {}, "test": {"after": ["build"]}}))
    with pytest.raises(ValueError, match="completed dependencies"):
        await planner.revise(result_type(step="test", succeeded=True))
    assert (await planner.view()).model_dump() == initial.model_dump()
    await planner.revise(result_type(step="build", succeeded=True))
    await planner.revise(result_type(step="test", succeeded=True))
    result = await planner.revise(result_type(step="build", succeeded=False))
    assert not any(node.done for node in result.nodes.values())
    invalid = result.model_copy(deep=True)
    invalid.nodes["build"].after = ["test"]
    with pytest.raises(ValueError, match="cycle"):
        await planner.revise(invalid)
    assert (await planner.view()).model_dump() == result.model_dump()


@pytest.mark.asyncio
async def test_multiple_consumers_delegate_to_one_owner_and_new_bindings_keep_it(example):
    owner = example("ChecklistPlanning")()
    delegate = example("DelegatingPlanning")
    tool, automatic = delegate(owner), delegate(owner)
    await tool.initialize("Verify")
    await tool.revise(example("StepResult")(step="Verify", succeeded=True))
    assert (await automatic.view()).items[0].done
    await automatic.revise(example("StepResult")(step="Verify", succeeded=False))
    resumed = delegate(owner)
    assert (await resumed.initialize("Continue")).model_dump() == (await tool.view()).model_dump()
    assert not (await resumed.view()).items[0].done
    independent = example("ChecklistPlanning")()
    with pytest.raises(RuntimeError, match="not initialized"):
        await independent.view()
