"""Native Curator exploration preserves input versions, permissions and stage handoffs."""

import asyncio
import json
from pathlib import Path

import pytest

from experimental.curator.generation.context.collect import collect
from experimental.curator.generation.run import GenerationError, Limits, generate
from experimental.curator.harness import Declaration, Validation
from experimental.curator.raven_adapter.exploration import Exploration, Withheld
from experimental.curator.raven_adapter.inspection import Inspection
from experimental.curator.raven_adapter.targets import catalogue
from raven.agent.tools.registry import ToolRegistry, call_failed
from raven.config.schema import Config
from raven.contracts.llm_provider import LLMResponse, ToolCallRequest
from raven.contracts.tool import Continuation, Tool, ToolResult
from raven.sandbox import DirectExecutor
from tests.test_harness_curator_generation import Provider, plan, response, selection


async def passing(candidate):
    return Validation([], [])


class Executor(DirectExecutor):
    """Stands in for an OS sandbox, the only executor exploration offers shell commands on."""

    def __init__(self):
        self.starts = self.stops = 0

    @property
    def is_sandboxed(self):
        return True

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1


@pytest.fixture
def exploration(tmp_path):
    repository = tmp_path / "repository"
    (repository / "raven").mkdir(parents=True)
    (repository / "raven/caller.py").write_text("from .helper import evidence\nvalue = evidence()\n")
    (repository / "raven/helper.py").write_text('def evidence():\n    return "UNREGISTERED_EVIDENCE"\n')
    config = Config()
    config.permissions.tools["exec"] = "allow"
    inspection = Inspection(Declaration("baseline", catalogue()), {"task": {"id": "task", "text": "Inspect"}}, {})
    return Exploration(config, inspection, repository=repository, source_paths=("raven",), executor=Executor())


def context(exploration):
    return collect(
        "Inspect",
        exploration.inspection.declaration,
        facts=exploration.inspection.facts,
        sources={},
        read_source=lambda **kwargs: {},
        exploration=exploration.describe(),
    )


@pytest.mark.asyncio
async def test_native_tools_discover_unregistered_files_and_read_shared_paths(exploration):
    executor = exploration.executor
    async with exploration:
        found = await exploration.registry.execute("grep", {"pattern": "evidence", "path": "source/raven"})
        assert "helper.py" in found and "caller.py" in found
        content = await exploration.registry.execute("read_file", {"path": "source/raven/helper.py"})
        assert "UNREGISTERED_EVIDENCE" in content
        assert executor.starts == 0
        output = await exploration.registry.execute("exec", {"command": "cat source/raven/helper.py"})
        assert "UNREGISTERED_EVIDENCE" in output
        assert executor.starts == 1
        facts = await exploration.registry.execute("read_file", {"path": "facts.json"})
        assert '"id": "task"' in facts
        exploration.verify()
    assert executor.stops == 1 and not exploration.root.exists()


@pytest.mark.asyncio
async def test_native_execution_refuses_outside_paths_background_and_denied_commands(exploration):
    async with exploration:
        for name, args in (
            ("read_file", {"path": str(exploration.repository / "raven/helper.py")}),
            ("exec", {"command": "printf test", "run_in_background": True}),
            ("exec", {"command": "printf test", "machine": "remote"}),
        ):
            result = await exploration.registry.execute(name, args)
            assert call_failed(result), str(result)
        assert exploration.executor.starts == 0
        exploration.config.permissions.tools["exec"] = "deny"
        result = await exploration.registry.execute("exec", {"command": "printf test"})
        assert call_failed(result) and result.blocks_call
        assert exploration.executor.starts == 0


@pytest.mark.parametrize("change", ["original", "source", "facts", "new_original", "new_snapshot"])
@pytest.mark.asyncio
async def test_changed_inputs_are_rejected_before_handoff(exploration, change):
    async with exploration:
        path = {
            "original": exploration.repository / "raven/helper.py",
            "source": exploration.root / "source/raven/helper.py",
            "facts": exploration.root / "facts.json",
            "new_original": exploration.repository / "raven/new.py",
            "new_snapshot": exploration.root / "source/raven/new.py",
        }[change]
        path.write_text("changed")
        with pytest.raises(ValueError, match="changed"):
            exploration.verify()


@pytest.mark.asyncio
async def test_preflight_stages_readable_draft_and_final_submission_is_checked_again(exploration):
    draft = {"values": {"action.config": {"temperature": 0.2}}, "files": {"policy.py": "POLICY = 'draft'\n"}}
    inspected = []

    class Curator(Provider):
        async def chat_with_retry(self, **kwargs):
            if len(self.requests) == 3:
                result = json.loads(kwargs["messages"][-2]["content"])
                package = Path(result["candidate"]["package"])
                self.responses.insert(0, response("read_file", {"path": str(package / "policy.py")}))
            return await super().chat_with_retry(**kwargs)

    provider = Curator(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("check_candidate", draft),
        response("submit_artifact", draft),
    )

    async def validate(candidate):
        inspected.append(candidate)
        return Validation([], [{"kind": "construction_only"}])

    async with exploration:
        result = await generate(
            context(exploration),
            provider,
            validate=validate,
            tool_registry=exploration.registry,
            stage_candidate=exploration.stage_candidate,
            limits=Limits(max_checks=1),
        )
        assert len(inspected) == 2 and inspected[0].artifact == inspected[1].artifact
        assert next(row for row in result.trace if row["event"] == "preflight")["arguments"] == draft
        assert any(row["event"] == "query" and "POLICY" in str(row["result"]) for row in result.trace)
        assert any(row["event"] == "preflight" and row["result"]["passed"] for row in result.trace)
        for request in provider.requests[2:]:
            definitions = {row["function"]["name"]: row["function"]["parameters"] for row in request["tools"]}
            assert definitions["check_candidate"] == definitions["submit_artifact"]
            schemas = json.loads(request["messages"][3]["content"])["submission_schemas"]
            assert schemas["check_candidate"] == schemas["submit_artifact"]


@pytest.mark.asyncio
async def test_preflight_uses_separate_budget_without_consuming_repair_or_query_budget(exploration):
    draft = {"values": {"action.config": {"temperature": 0.2}}}
    provider = Provider(
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("check_candidate", draft),
        response("check_candidate", draft),
        response("submit_artifact", draft),
    )
    checks = []

    async def validate(candidate):
        checks.append(candidate)
        return Validation([], [])

    async with exploration:
        result = await generate(
            context(exploration), provider, validate=validate, limits=Limits(max_checks=1, max_queries=0, max_repairs=0)
        )
    assert len(checks) == 2
    assert any(row["event"] == "preflight" and "budget exhausted" in str(row["result"]) for row in result.trace)


class Refusal(Tool):
    name = "refuse"
    description = "Return a native execution refusal."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, continuation):
        self.continuation = continuation

    async def execute(self, **kwargs):
        return ToolResult("REFUSED", blocks_call=True, continuation=self.continuation, retryable=False, ok=False)


@pytest.mark.parametrize("continuation", [Continuation.CONTINUE, Continuation.ABORT_TURN])
@pytest.mark.asyncio
async def test_blocking_native_result_skips_siblings_and_honors_continuation(exploration, continuation):
    registry = ToolRegistry()
    registry.register(Refusal(continuation))
    calls = LLMResponse(
        content=None,
        tool_calls=[
            ToolCallRequest("refusal", "refuse", {}),
            ToolCallRequest("skipped", "read_fact", {"name": "task"}),
        ],
    )
    provider = Provider(calls, response("submit_selection", selection()))
    async with exploration:
        if continuation == Continuation.ABORT_TURN:
            with pytest.raises(GenerationError, match="stopped by native") as error:
                await generate(context(exploration), provider, validate=passing, tool_registry=registry)
            trace = error.value.trace
            assert len(provider.requests) == 1
        else:
            result = await generate(
                context(exploration), provider, validate=passing, tool_registry=registry, limits=Limits(max_queries=1)
            )
            trace = result.trace
            messages = provider.requests[1]["messages"]
            assert any(row.get("tool_call_id") == "skipped" for row in messages)
        assert any(row["event"] == "tool.skipped" for row in trace)
        assert next(row for row in trace if row["event"] == "query")["result"]["retryable"] is False


@pytest.mark.asyncio
async def test_cancellation_during_executor_start_releases_the_owned_environment(exploration):
    entered = asyncio.Event()

    async def starting():
        entered.set()
        await asyncio.Future()

    exploration.executor.start = starting

    async def run():
        async with exploration:
            await exploration.registry.execute("exec", {"command": "printf test"})

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exploration.executor.stops == 1 and not exploration.root.exists()


@pytest.mark.asyncio
async def test_native_read_keeps_its_trust_boundary_across_both_stage_handoffs(exploration):
    provider = Provider(
        response("read_file", {"path": "source/raven/helper.py"}),
        response("submit_selection", selection("action.config")),
        response("submit_plan", plan("action.config")),
        response("submit_artifact", {"values": {"action.config": {"temperature": 0.2}}}),
    )

    async def validate(candidate):
        return Validation([], [])

    async with exploration:
        result = await generate(context(exploration), provider, validate=validate, tool_registry=exploration.registry)
    recorded = next(row for row in result.trace if row["event"] == "query")
    original_tool_text = next(
        message["content"]
        for message in provider.requests[1]["messages"]
        if message.get("role") == "tool" and message.get("name") == "read_file"
    )
    assert "BEGIN UNTRUSTED" in original_tool_text and "UNREGISTERED_EVIDENCE" in original_tool_text
    assert recorded["result"]["text"] == original_tool_text
    for request in provider.requests[2:]:
        stage_data = json.loads(request["messages"][3]["content"])
        assert next(row for row in stage_data["history"] if row["event"] == "query") == recorded


@pytest.mark.asyncio
async def test_exhausted_query_budget_never_starts_native_execution(exploration):
    provider = Provider(
        response("exec", {"command": "echo MUST_NOT_RUN"}),
        response("submit_selection", selection()),
    )
    async with exploration:
        result = await generate(
            context(exploration),
            provider,
            validate=passing,
            tool_registry=exploration.registry,
            limits=Limits(max_queries=0),
        )
        assert exploration.executor.starts == 0
    assert any(row["event"] == "query.rejected" for row in result.trace)
    assert not result.candidate.plan.changes


@pytest.mark.asyncio
async def test_reopened_exploration_retains_scratch_draft_and_exact_paths(exploration, tmp_path):
    root = tmp_path / "retained"
    async with Exploration(
        exploration.config,
        exploration.inspection,
        repository=exploration.repository,
        source_paths=exploration.source_paths,
        root=root,
        executor=Executor(),
    ) as first:
        (root / "scratch.txt").write_text("retained evidence")
        candidate = first.inspection.declaration.accept(
            plan("action.config"),
            {"values": {"action.config": {"temperature": 0.2}}, "files": {"draft.py": "VALUE = 2\n"}},
        )
        location = first.stage_candidate(candidate)
        await first.start_executor()
    assert first.executor.stops == 1 and root.exists()
    async with Exploration(
        exploration.config,
        exploration.inspection,
        repository=exploration.repository,
        source_paths=exploration.source_paths,
        root=root,
        executor=Executor(),
    ) as second:
        assert second.describe() == first.describe()
        assert (root / "scratch.txt").read_text() == "retained evidence"
        assert (Path(location["package"]) / "draft.py").read_text() == "VALUE = 2\n"
        assert second.executor.starts == 0
        second.verify()
    async with exploration:
        pass


@pytest.mark.asyncio
async def test_live_edits_to_reference_sources_keep_the_snapshot_while_runtime_edits_still_refuse(tmp_path):
    repository = tmp_path / "repository"
    (repository / "raven").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "raven/helper.py").write_text("VALUE = 1\n")
    (repository / "tests/test_helper.py").write_text("assert True\n")
    inspection = Inspection(Declaration("baseline", catalogue()), {"task": {"id": "task", "text": "Inspect"}}, {})
    async with Exploration(Config(), inspection, repository=repository, source_paths=("raven", "tests")) as exploration:
        (repository / "tests/test_helper.py").write_text("assert 1 == 1\n")
        (repository / "tests/test_new.py").write_text("assert True\n")
        exploration.verify()
        assert (exploration.root / "source/tests/test_helper.py").read_text() == "assert True\n"
        assert not (exploration.root / "source/tests/test_new.py").exists()
        (repository / "raven/helper.py").write_text("VALUE = 2\n")
        with pytest.raises(ValueError, match="curation input changed"):
            exploration.verify()
        (repository / "raven/helper.py").write_text("VALUE = 1\n")
        (repository / "raven/extra.py").write_text("EXTRA = True\n")
        with pytest.raises(ValueError, match="source inventory changed"):
            exploration.verify()
        (repository / "raven/extra.py").unlink()
        (exploration.root / "source/tests/test_helper.py").write_text("tampered\n")
        with pytest.raises(ValueError, match="curation input changed"):
            exploration.verify()
        (exploration.root / "source/tests/test_helper.py").write_text("assert True\n")


@pytest.mark.asyncio
async def test_reopened_exploration_refuses_changed_source(exploration, tmp_path):
    root = tmp_path / "retained"
    async with Exploration(
        exploration.config,
        exploration.inspection,
        repository=exploration.repository,
        source_paths=exploration.source_paths,
        root=root,
    ):
        pass
    (root / "source/raven/helper.py").write_text("CHANGED = True\n")
    with pytest.raises(ValueError, match="changed"):
        Exploration(
            exploration.config,
            exploration.inspection,
            repository=exploration.repository,
            source_paths=exploration.source_paths,
            root=root,
        )
    assert root.exists()
    async with exploration:
        pass


@pytest.mark.asyncio
async def test_registered_file_outside_an_existing_mount_remains_readable_without_widening_its_parent(tmp_path):
    from experimental.curator.harness import Declaration
    from experimental.curator.raven_adapter.exploration import Exploration
    from experimental.curator.raven_adapter.inspection import Inspection, file_source
    from raven.config.schema import Config

    repository = tmp_path / "repository"
    narrow = repository / "experimental/curator"
    narrow.mkdir(parents=True)
    (narrow / "module.py").write_text("PUBLIC = True\n")
    shared = narrow.parent / "requirements.py"
    shared.write_text("SHARED_CONTRACT = True\n")
    unrelated = narrow.parent / "unregistered.txt"
    unrelated.write_text("outside the declared materials")
    source = {**file_source(shared), "root": str(narrow.parent)}
    inspection = Inspection(Declaration("baseline", ()), {}, {"shared": source})
    async with Exploration(
        Config(), inspection, repository=repository, source_paths=("experimental/curator",)
    ) as exploration:
        assert exploration.read_source(name="shared")["text"] == "SHARED_CONTRACT = True"
        assert (exploration.root / "source/experimental/requirements.py").read_text() == shared.read_text()
        assert not (exploration.root / "source/experimental/unregistered.txt").exists()
        exploration.verify()


@pytest.mark.asyncio
async def test_without_an_os_sandbox_no_shell_is_offered_and_commands_never_leave_the_workspace(tmp_path):
    repository = tmp_path / "repository"
    (repository / "raven").mkdir(parents=True)
    (repository / "raven/helper.py").write_text("VALUE = 1\n")
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "answer.txt").write_text("PRIVATE_ANSWER")
    config = Config()
    config.permissions.tools["exec"] = "allow"
    inspection = Inspection(Declaration("baseline", catalogue()), {"task": {"id": "task", "text": "Inspect"}}, {})
    plain = Exploration(config, inspection, repository=repository, source_paths=("raven",), executor=DirectExecutor())
    async with plain:
        assert "exec" not in plain.registry.tool_names and plain.describe()["shell"].startswith("not offered")
        refused = await plain.registry.execute("read_file", {"path": str(outside / "answer.txt")})
        assert call_failed(refused) and "PRIVATE_ANSWER" not in str(refused)
    sandboxed = Exploration(config, inspection, repository=repository, source_paths=("raven",), executor=Executor())
    async with sandboxed:
        assert "working_dir" not in sandboxed.registry.get("exec").parameters["properties"]
        moved = await sandboxed.registry.execute("exec", {"command": "cat answer.txt", "working_dir": str(outside)})
        assert call_failed(moved) and "PRIVATE_ANSWER" not in str(moved)


def test_the_evaluation_side_the_caller_withholds_never_enters_the_snapshot(tmp_path):
    repository = tmp_path / "repository"
    (repository / "raven").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "raven/helper.py").write_text("VALUE = 1\n")
    (repository / "tests/test_raven_helper.py").write_text("def test_value(): pass\n")
    (repository / "tests/test_simulation_reference.py").write_text("QUOTE = 'HL-Q-1026-4P'\n")
    (repository / "tests/test_cards.py").write_text("from experimental.simulation.cards import draw\n")
    inspection = Inspection(Declaration("baseline", catalogue()), {"task": {"id": "task", "text": "Inspect"}}, {})
    withheld = Withheld(paths=("tests/test_simulation_*",), markers=(b"experimental.simulation",))
    copies = {}
    for name, value in (("open", Withheld()), ("withheld", withheld)):
        exploration = Exploration(
            Config(),
            inspection,
            repository=repository,
            source_paths=("raven", "tests"),
            withheld=value,
            executor=Executor(),
        )
        copies[name] = {path.name for path in (exploration.root / "source" / "tests").iterdir()}
        exploration.verify()
        exploration.temporary.cleanup()
    assert copies["open"] == {"test_raven_helper.py", "test_simulation_reference.py", "test_cards.py"}
    assert copies["withheld"] == {"test_raven_helper.py"}
