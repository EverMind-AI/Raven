"""Content ownership across root and managed child homes rejects ambiguous writers."""

from types import SimpleNamespace

import pytest

from experimental.curator.harness import Artifact, Task
from experimental.curator.raven_adapter.baselines import Baseline
from experimental.curator.raven_adapter.deployment import Child, check_content_owners
from raven.config.raven import RavenConfig
from raven.config.schema import Config


def baseline(home):
    config = Config()
    config.agents.defaults.workspace = str(home)
    return Baseline(config, RavenConfig(), home.parent, task=Task(text="Work"))


def test_root_content_may_not_reach_into_a_managed_child_home(tmp_path):
    root = baseline(tmp_path / "root")
    child = Child(
        baseline(tmp_path / "root/subagents/Hosted"),
        Artifact(values={"memory.prompt": {"TOOLS.md": "SOP"}, "planning.skills": {"check/SKILL.md": "knowledge"}}),
    )
    authored = Artifact(values={"memory.prompt": {"TOOLS.md": "root SOP"}, "planning.skills": {"check/SKILL.md": "x"}})
    check_content_owners(root, authored, {"Hosted": child})
    nested = Child(baseline(tmp_path / "root/skills/Hosted"))
    with pytest.raises(ValueError, match="root content overlaps"):
        check_content_owners(root, Artifact(values={"planning.skills": {"Hosted/SKILL.md": "x"}}), {"Hosted": nested})


def test_shared_home_does_not_allow_two_content_writers(tmp_path):
    root = baseline(tmp_path / "root")
    first = Child(baseline(tmp_path / "shared"), Artifact(values={"memory.prompt": {"TOOLS.md": "one"}}))
    second = Child(baseline(tmp_path / "shared"), Artifact(values={"memory.prompt": {"TOOLS.md": "two"}}))
    with pytest.raises(ValueError, match="both own"):
        check_content_owners(root, Artifact(values={}), {"first": first, "second": second})


def test_feedback_location_routes_requirements_without_discarding_original_signals():
    from experimental.curator.composition.requirements import feedback_for

    feedback = {
        "signals": ["supplied document"],
        "requirements": [
            {"behavior": "root", "locations": ["root"]},
            {"behavior": "child", "locations": ["child/Hosted"]},
            {"behavior": "unknown", "locations": []},
        ],
    }
    selected = feedback_for("Hosted", feedback)
    assert [item["behavior"] for item in selected["requirements"]] == ["child", "unknown"]
    assert selected["signals"] == feedback["signals"]
    assert len(feedback["requirements"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_activation_restores_the_checkpoint_flushed_by_shutdown(tmp_path, cleanup_fails):
    import asyncio
    from unittest.mock import AsyncMock

    from experimental.curator.harness import Declaration
    from experimental.curator.raven_adapter.deployment import activate
    from experimental.curator.raven_adapter.inspection import Inspection
    from experimental.curator.raven_adapter.targets import catalogue
    from tests.test_harness_curator_generation import plan

    root = tmp_path / "runtime"
    root.mkdir()
    state = root / "planning.json"
    state.write_text('{"version": "before shutdown"}')
    declaration = Declaration("old", catalogue())
    candidate = declaration.accept(plan("action.config"), {"values": {"action.config": {"temperature": 0.2}}})
    worker = SimpleNamespace(
        baseline=baseline(tmp_path / "home"),
        root=root,
        children={},
        artifact=Artifact(values={}),
        _lock=asyncio.Lock(),
        _content_baseline={},
        _exchange=AsyncMock(return_value=True),
        _inspect=AsyncMock(return_value=Inspection(declaration, {}, {})),
        _accept=lambda candidate, inspection: candidate,
        _release_edited=lambda submitted, proposed, declaration: proposed,
        _retired_content=lambda proposed, declaration: {},
    )
    closed = 0

    async def close():
        nonlocal closed
        closed += 1
        if closed == 1:
            state.write_text('{"version": "flushed"}')
        elif cleanup_fails:
            raise RuntimeError("Candidate cleanup failed")

    async def start():
        if worker.artifact.values:
            state.write_text('{"version": "failed candidate"}')
            raise RuntimeError("Candidate start failed")
        assert state.read_text() == '{"version": "flushed"}'

    worker._close, worker._start = close, start
    with pytest.raises(RuntimeError, match="Candidate (start|cleanup) failed"):
        await activate(worker, candidate)
    assert worker.artifact.values == {} and state.read_text() == '{"version": "flushed"}'
