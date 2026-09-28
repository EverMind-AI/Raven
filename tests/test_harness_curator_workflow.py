"""Paused curation retains its workspace and resumes without replaying host effects."""

import json
import shutil
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from experimental.curator import workflow
from experimental.curator.generation.run import GenerationPausedError, Limits
from experimental.curator.harness import Artifact, Declaration, Validation
from experimental.curator.raven_adapter.exploration import Exploration, Withheld
from experimental.curator.raven_adapter.inspection import Inspection
from experimental.curator.raven_adapter.targets import catalogue
from raven.config.schema import Config
from tests.test_harness_curator_generation import Provider, plan, response, selection


@pytest.fixture
def worker(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    (repository / "raven").mkdir(parents=True)
    (repository / "raven/evidence.py").write_text("EVIDENCE = 'retained'\n")
    monkeypatch.setattr(workflow, "Exploration", partial(Exploration, repository=repository, source_paths=("raven",)))
    inspection = Inspection(Declaration("baseline", catalogue()), {"task": {"id": "task", "text": "Improve"}}, {})
    worker = SimpleNamespace(
        root=tmp_path / "worker",
        baseline=SimpleNamespace(config=Config()),
        last_plan=None,
        last_execution=None,
        artifact=Artifact(values={}),
        inspect=AsyncMock(return_value=inspection),
        check=AsyncMock(return_value=Validation([], [])),
        install=AsyncMock(),
        withheld=Withheld(),
    )
    yield worker
    pending = worker.root / "curation/pending.json"
    if pending.exists():
        shutil.rmtree(json.loads(pending.read_text())["workspace"], ignore_errors=True)


@pytest.mark.asyncio
async def test_workflow_resumes_preflight_with_same_files_and_cleans_up_after_installation(worker):
    draft = {"values": {"action.config": {"temperature": 0.2}}, "files": {"draft.py": "VALUE = 2\n"}}
    first = Provider(
        response("read_file", {"path": "source/raven/evidence.py"}),
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("check_candidate", draft),
    )
    with pytest.raises(GenerationPausedError):
        await workflow.improve(worker, first, limits=Limits(max_calls=4), feedback={"text": "Keep evidence"})
    live = json.loads((worker.root / "progress" / "curation.json").read_text())
    assert live["finished"] is True and "budget" in live["error"]
    assert any(event["event"] == "preflight" for event in live["events"])
    path = worker.root / "curation/pending.json"
    pending = json.loads(path.read_text())
    workspace = Path(pending["workspace"])
    assert pending["state"]["stage"] == "implement"
    assert pending["state"]["checks"] == 1
    assert list((workspace / "candidate").rglob("draft.py"))
    (workspace / "scratch.txt").write_text("retained scratch")
    worker.install.assert_not_awaited()
    resumed = Provider(response("read_file", {"path": "scratch.txt"}), response("submit_artifact", draft))
    result = await workflow.improve(worker, resumed, limits=Limits(max_calls=6))
    assert "retained scratch" in str(result.trace)
    assert "Keep evidence" in resumed.requests[0]["messages"][1]["content"]
    assert worker.check.await_count == 2 and worker.install.await_count == 1
    assert not path.exists() and not workspace.exists()
    assert [event["call"] for event in result.trace if event["event"] == "model.call"] == list(range(1, 7))
    progress = json.loads((worker.root / "progress" / "curation.json").read_text())
    assert progress["finished"] is True and progress["error"] is None and progress["updated"] >= progress["started"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["feedback", "missing_workspace", "source", "limits"])
async def test_refused_resume_preserves_checkpoint_without_calling_model(worker, change):
    with pytest.raises(GenerationPausedError):
        await workflow.improve(
            worker, Provider(response("read_file", {"path": "facts.json"})), limits=Limits(max_calls=1)
        )
    path = worker.root / "curation/pending.json"
    before = path.read_bytes()
    workspace = Path(json.loads(before)["workspace"])
    arguments = {"limits": Limits(max_calls=2)}
    if change == "feedback":
        arguments["feedback"] = "Different intent"
    elif change == "missing_workspace":
        shutil.rmtree(workspace)
    elif change == "source":
        (workspace / "source/raven/evidence.py").write_text("CHANGED = True\n")
    else:
        arguments["limits"] = Limits(max_calls=2, max_queries=0)
    provider = Provider()
    with pytest.raises(ValueError):
        await workflow.improve(worker, provider, **arguments)
    assert path.read_bytes() == before and not provider.requests
    if change == "missing_workspace":
        assert not workspace.exists()


@pytest.mark.asyncio
async def test_explicit_restart_discards_old_investigation_and_workspace(worker):
    with pytest.raises(GenerationPausedError):
        await workflow.improve(
            worker, Provider(response("read_file", {"path": "facts.json"})), limits=Limits(max_calls=1)
        )
    path = worker.root / "curation/pending.json"
    workspace = Path(json.loads(path.read_text())["workspace"])
    result = await workflow.improve(worker, Provider(response("submit_selection", selection())), resume=False)
    assert not path.exists() and not workspace.exists()
    assert not any(event["event"] == "query" for event in result.trace)


@pytest.mark.asyncio
async def test_proposal_retains_its_candidate_and_investigation_without_installing(worker):
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )
    result = await workflow.propose(worker, provider)
    worker.install.assert_not_awaited()
    held = json.loads((worker.root / "curation/candidate.json").read_text())
    assert held["state"]["candidate"]["artifact"]["values"] == result.candidate.artifact.values
    workspace = Path(held["workspace"])
    assert (workspace / "snapshot.json").is_file()
    state = json.loads((worker.root / "progress/generation.json").read_text())
    assert state["calls"] == 3
    repaired = await workflow.propose(
        worker,
        Provider(response("submit_artifact", {"values": {"action.config": {"temperature": 0.3}}})),
        repair=Validation(["Another scope requires a compatible sampling choice"], []),
        limits=Limits(max_calls=4),
    )
    assert repaired.candidate.artifact.values["action.config"]["temperature"] == 0.3
    assert json.loads((worker.root / "progress/generation.json").read_text())["calls"] == 4
    worker.install.assert_not_awaited()
    shutil.rmtree(workspace)


@pytest.mark.asyncio
async def test_provider_interruption_retains_investigation_and_resumes_the_current_stage(worker):
    from experimental.curator.generation.run import GenerationInterruptedError
    from raven.contracts.llm_provider import LLMResponse

    first = Provider(
        response("read_file", {"path": "source/raven/evidence.py"}),
        response("submit_selection", selection("action.config")),
        LLMResponse(content="Transient provider failure", finish_reason="error"),
    )
    with pytest.raises(GenerationInterruptedError) as interrupted:
        await workflow.improve(worker, first, limits=Limits(max_calls=5))
    assert interrupted.value.state.calls == 3
    path = worker.root / "curation/pending.json"
    saved = json.loads(path.read_text())
    workspace = Path(saved["workspace"])
    assert saved["state"]["stage"] == "design"
    assert (workspace / "snapshot.json").is_file()
    resumed = Provider(
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )
    result = await workflow.improve(worker, resumed, limits=Limits(max_calls=5))
    assert len(resumed.requests) == 2
    assert "retained" in str(result.trace)
    assert [row["call"] for row in result.trace if row["event"] == "model.call"] == [1, 2, 3, 4, 5]
    assert not path.exists() and not workspace.exists()
