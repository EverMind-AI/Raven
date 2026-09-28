"""Real Raven worker and generated Harness pipeline checks in isolated processes."""

from copy import deepcopy

import pytest

from experimental.curator.harness import Change, Plan
from experimental.curator.raven_adapter.inspection import Baseline
from experimental.curator.raven_adapter.worker import Worker
from raven.config.raven import RavenConfig
from raven.config.schema import Config
from raven.contracts.llm_provider import GenerationSettings
from raven.core.plugin_stack import discover_plugins
from raven.providers.base import LLMProvider, LLMResponse
from tests.test_harness_curator_generation import response, selection


class ReplayProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__(api_key="test")
        self.responses = list(deepcopy(responses))

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return self.responses.pop(0) if self.responses else LLMResponse(content="done")

    def get_default_model(self):
        return "openai/gpt-4o-mini"


def replay_provider(config, responses=()):
    provider = ReplayProvider(responses)
    provider.generation = GenerationSettings(
        temperature=config.agents.defaults.temperature,
        reasoning_effort=config.agents.defaults.reasoning_effort,
    )
    return provider


@pytest.fixture
def baseline(tmp_path):
    home = tmp_path / "agent"
    workdir = tmp_path / "work"
    home.mkdir()
    workdir.mkdir()
    config = Config()
    config.agents.defaults.workspace = str(home)
    config.agents.defaults.model = "openai/gpt-4o-mini"
    config.agents.defaults.context_window_tokens = 128000
    config.agents.defaults.max_tool_iterations = 4
    extension = RavenConfig(memory={"backend": None})
    extension.plugins.disabled = [item.manifest.id for item in discover_plugins(extension)]
    extension.skill_forge.router.hub.endpoint = ""
    return Baseline(config, extension, workdir)


def plan_for(*names):
    return Plan(
        design="Invoke the selected native bindings and verify their observable effects.",
        understanding="Improve the worker",
        changes=tuple(
            Change(target=name, reason="Task requirement", expected="Native effect", verification="Observe the runtime")
            for name in names
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_inspection_and_conversation_survive_multiple_turns(baseline, tmp_path):
    worker = Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30)
    async with worker:
        inspection = await worker.inspect()
        assert inspection.facts["backend"] == "AgentLoop"
        assert inspection.declaration.target("capability.tools")
        source = inspection.read_source("loop.participant_timing", length=1000)
        assert "ParticipantHook" in source["text"]
        first = await worker.run("Remember the label cobalt.")
        second = await worker.run("Repeat the earlier label.")
        assert not first.errors and not second.errors
        requests = [row for row in second.records if row["kind"] == "provider.request"]
        assert "cobalt" in str(requests[0])
    assert worker._process is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generated_participant_changes_the_real_model_input(baseline, tmp_path):
    worker = Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30)
    async with worker:
        inspection = await worker.inspect()
        plan = plan_for("memory.system_addendum")
        candidate = inspection.declaration.accept(
            plan,
            {
                "values": {"memory.system_addendum": "behavior:Participant"},
                "files": {
                    "behavior.py": (
                        "from raven.contracts.participant import AgentParticipant, Intake\n"
                        "class Participant(AgentParticipant):\n"
                        "    async def system_addendum(self, step):\n"
                        "        return Intake('CURATOR_RUNTIME_MARKER')\n"
                    )
                },
            },
        )
        report = await worker.check(candidate)
        assert report.passed, report.errors
        await worker.install(candidate)
        result = await worker.run("Answer briefly.")
        assert not result.errors
        assert any(row["kind"] == "participant.result" for row in result.records)
        assert any("CURATOR_RUNTIME_MARKER" in str(row) for row in result.records if row["kind"] == "provider.request")


COMPOSED_CODE = """
from raven.contracts.tool import Tool
from raven.contracts.participant import AgentParticipant, Resample

seen = False

class Probe(Tool):
    name = "probe"
    description = "Return a local execution receipt."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def execute(self, value):
        global seen
        seen = True
        return "EVIDENCE:" + value

def make_tool(context):
    return Probe()

class Participant(AgentParticipant):
    async def review(self, step):
        if step.phase == "after_iteration" and not step.response.tool_calls and not seen:
            return Resample("Evidence is missing", inject=[{"role": "user", "content": "Call probe before finishing."}])
        return None
"""

GATE_CODE = """
class Gate:
    name = "guard"

    async def adjudicate(self, name, params, *, session_workdir):
        return "GATE_REFUSED" if name == "probe" else None

def make(context):
    return Gate()
"""

LIFECYCLE_CODE = """
from raven.contracts.memory import Memory

def append(path, value):
    with path.open("a") as stream:
        stream.write(value + "\\n")

class Backend:
    def __init__(self, context):
        self.path = context.services.workspace / "lifecycle.txt"

    async def start(self):
        append(self.path, "backend.start")

    async def stop(self):
        append(self.path, "backend.stop")

    async def recall(self, query, **kwargs):
        append(self.path, "backend.recall")
        return [Memory(text="MEMORY_RUNTIME_MARKER", score=1)]

    async def store(self, session_id, messages, **kwargs):
        append(self.path, "backend.store")
        return True

    async def feedback(self, signals):
        pass

class Service:
    def __init__(self, context):
        self.path = context.services.workspace / "lifecycle.txt"

    async def start(self, handles):
        append(self.path, "service.start")

    async def stop(self):
        append(self.path, "service.stop")

class Observer:
    def __init__(self, context):
        self.path = context.services.workspace / "lifecycle.txt"

    def on_session_deleted(self, session_key, removed):
        append(self.path, "observer.deleted")

def backend(context):
    return Backend(context)

def service(context):
    return Service(context)

def observer(context):
    return Observer(context)
"""


async def exercise_turn(bound):
    from raven.agent.spine_runner import AgentTurnRunner
    from raven.spine import ChatType, Source, TurnRequest

    async def emit(event):
        bound.recorder.add("probe.event", event=event)

    await AgentTurnRunner(bound.runtime.loop, stream=False).run(
        TurnRequest(
            origin=bound.baseline.origin,
            source=Source(channel="curator", chat_id="probe", sender_id="user", chat_type=ChatType.DM),
            text="Complete the task using available evidence.",
            conversation="curator:probe",
        ),
        emit,
        lambda: [],
    )
    bound.observer.finish()


async def exercise_lifecycle(bound):
    await bound.start()
    session = bound.runtime.loop.sessions.get_or_create("curator:retire")
    bound.runtime.loop.sessions.save(session)
    bound.runtime.loop.sessions.delete("curator:retire")
    text = (bound.baseline.config.workspace_path / "lifecycle.txt").read_text()
    assert "observer.deleted" in text
    bound.recorder.add("probe.observer", log=text)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generation_queries_plans_repairs_and_runs_a_composed_harness(baseline, tmp_path):
    from functools import partial

    from experimental.curator.generation.context.collect import collect
    from experimental.curator.generation.run import generate
    from raven.contracts.llm_provider import ToolCallRequest

    baseline.config.permissions.tools["probe"] = "allow"
    responses = (
        LLMResponse(content="premature answer"),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("probe", "probe", {"value": "checked"})]),
        LLMResponse(content="done with proof"),
    )
    factory = partial(replay_provider, responses=responses)
    worker = Worker(baseline, tmp_path / "runtime", provider_factory=factory, timeout=30)
    async with worker:
        inspection = await worker.inspect()
        context = collect(
            "Complete the task with execution evidence.",
            inspection.declaration,
            facts=inspection.facts,
            sources=inspection.sources,
            read_source=inspection.read_source,
        )
        plan = plan_for("action.config", "memory.prompt", "planning.skills", "capability.tools", "action.review")
        good = {
            "values": {
                "action.config": {"temperature": 0.25},
                "memory.prompt": {"TOOLS.md": "BOOTSTRAP_RUNTIME_MARKER"},
                "planning.skills": {
                    "evidence/SKILL.md": "---\nname: evidence\ndescription: Inspect execution receipts\n---\nSKILL_RUNTIME_MARKER"
                },
                "capability.tools": [{"name": "probe", "factory": "mechanism:make_tool"}],
                "action.review": "mechanism:Participant",
            },
            "files": {"mechanism.py": COMPOSED_CODE},
        }
        bad = deepcopy(good)
        bad["files"]["mechanism.py"] = "def invalid(:"
        planner = ReplayProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            "query", "read_source", {"name": "loop.participant_timing", "find": "after_iteration"}
                        )
                    ],
                ),
                response("submit_selection", selection(*(change.target for change in plan.changes))),
                LLMResponse(
                    content=None, tool_calls=[ToolCallRequest("plan", "submit_plan", plan.model_dump(mode="json"))]
                ),
                LLMResponse(content=None, tool_calls=[ToolCallRequest("bad", "submit_artifact", bad)]),
                LLMResponse(content=None, tool_calls=[ToolCallRequest("good", "submit_artifact", good)]),
            ]
        )
        generated = await generate(context, planner, validate=partial(worker.check, probe=exercise_turn))
        assert generated.validation.passed
        assert any(row["event"] == "validation" and row["errors"] for row in generated.trace)
        assert any(row["kind"] == "participant.result" for row in generated.validation.observations)
        assert not (baseline.config.workspace_path / "TOOLS.md").exists(), "preflight must not change the live home"
        current = await worker.install(generated.candidate)
        assert any(row["name"] == "evidence" for row in current.facts["skills"])
        result = await worker.run("Complete the task with execution evidence.")
        requests = [row for row in result.records if row["kind"] == "provider.request"]
        assert len(requests) == 3
        assert requests[0]["defaults"]["temperature"] == 0.25
        assert "BOOTSTRAP_RUNTIME_MARKER" in str(requests[0])
        assert "Call probe before finishing" in str(requests[1])
        assert any(row["kind"] == "loop.control" and row["rollbacks"] == 1 for row in result.records)
        assert any("EVIDENCE:checked" in str(row) for row in result.events)
        assert not result.errors


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generated_gate_uses_native_tool_adjudication(baseline, tmp_path):
    from functools import partial

    from raven.contracts.llm_provider import ToolCallRequest

    baseline.config.permissions.tools["probe"] = "allow"
    factory = partial(
        replay_provider,
        responses=(
            LLMResponse(content=None, tool_calls=[ToolCallRequest("tool", "probe", {"value": "test"})]),
            LLMResponse(content="done"),
        ),
    )
    async with Worker(baseline, tmp_path / "runtime", provider_factory=factory, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("capability.tools", "action.tool_gates"),
            {
                "values": {
                    "capability.tools": [{"name": "probe", "factory": "mechanism:make_tool"}],
                    "action.tool_gates": [{"name": "guard", "factory": "gate:make"}],
                },
                "files": {"mechanism.py": COMPOSED_CODE, "gate.py": GATE_CODE},
            },
        )
        await worker.install(candidate)
        result = await worker.run("Use the probe.")
        assert "GATE_REFUSED" in str(result.events)
        assert "EVIDENCE:test" not in str(result.events)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generated_backend_services_and_observers_follow_native_lifecycle(baseline, tmp_path):
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("memory.backends", "memory.backend_config", "action.services", "memory.session_observers"),
            {
                "values": {
                    "memory.backends": [{"name": "curated", "factory": "lifecycle:backend"}],
                    "memory.backend_config": {"backend": "curated"},
                    "action.services": [{"name": "service", "factory": "lifecycle:service"}],
                    "memory.session_observers": [{"name": "observer", "factory": "lifecycle:observer"}],
                },
                "files": {"lifecycle.py": LIFECYCLE_CODE},
            },
        )
        report = await worker.check(candidate, probe=exercise_lifecycle)
        assert report.passed, report.errors
        assert not (baseline.config.workspace_path / "lifecycle.txt").exists()
        await worker.install(candidate)
        result = await worker.run("Recall relevant information.")
        assert any("MEMORY_RUNTIME_MARKER" in str(row) for row in result.records if row["kind"] == "provider.request")
    log = (baseline.config.workspace_path / "lifecycle.txt").read_text()
    assert "backend.start" in log and "backend.stop" in log
    assert "service.start" in log and "service.stop" in log


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_engine_instance_is_used_by_the_native_loop(baseline, tmp_path):
    code = """
from raven.contracts.context import ContextEngine
from raven.contracts.assembled import AssembledContext

class Engine:
    name = "generated-context"
    owns_compaction = True

    def set_provider(self, provider, model):
        pass

    async def after_turn(self, session_key, outcome):
        pass

    async def assemble(self, session_key, session_messages, budget, *, turn):
        return AssembledContext(messages=[
            {"role": "system", "content": "CONTEXT_ENGINE_RUNTIME_MARKER"},
            *session_messages,
            {"role": "user", "content": turn.current_message},
        ])

def make(context):
    return Engine()
"""
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("memory.context_engine"),
            {
                "values": {"memory.context_engine": "context:make"},
                "files": {"context.py": code},
            },
        )
        await worker.install(candidate)
        result = await worker.run("Answer the task.")
        assert any(
            "CONTEXT_ENGINE_RUNTIME_MARKER" in str(row) for row in result.records if row["kind"] == "provider.request"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_service_activation_restores_content_and_the_previous_worker(baseline, tmp_path):
    from experimental.curator.raven_adapter.worker import WorkerError

    path = baseline.config.workspace_path / "TOOLS.md"
    path.write_text("ORIGINAL_CONTENT")
    code = """
class Service:
    def __init__(self, path):
        self.path = path
    async def start(self, handles):
        self.path.write_text("allocated")
        raise RuntimeError("intentional startup failure")
    async def stop(self):
        self.path.write_text("stopped")
def make(context):
    return Service(context.services.workspace / "failed_service.txt")
"""
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("memory.prompt", "action.services"),
            {
                "values": {
                    "memory.prompt": {"TOOLS.md": "MUST_ROLL_BACK"},
                    "action.services": [{"name": "bad", "factory": "broken:make"}],
                },
                "files": {"broken.py": code},
            },
        )
        with pytest.raises(WorkerError, match="did not start"):
            await worker.install(candidate)
        assert path.read_text() == "ORIGINAL_CONTENT"
        assert (baseline.config.workspace_path / "failed_service.txt").read_text() == "stopped"
        result = await worker.run("Continue the original task.")
        assert any("ORIGINAL_CONTENT" in str(row) for row in result.records if row["kind"] == "provider.request")
        assert not worker.artifact.values


HOOK_CODE = """
from raven.contracts.loop_hooks import AgentHook, HookDecision

class Hook(AgentHook):
    def __init__(self, note):
        self.note = note

    async def before_iteration(self, context):
        return HookDecision(append_note=self.note)

def make(context):
    return Hook(context.config["note"])
"""

PARTICIPANT_CODE = """
from raven.contracts.participant import AgentParticipant, Intake

class Participant(AgentParticipant):
    async def intake(self, text, step):
        return Intake(text + " INTAKE_RUNTIME_MARKER")

    async def advise(self, step):
        return "ADVICE_RUNTIME_MARKER"

    async def select_tools(self, offered, step):
        return [tool for tool in offered if tool["function"]["name"] == "probe"]

    async def archive(self, step, reply):
        return {"curator": {"archived": True}}

    async def salvage(self, step):
        return "SALVAGE_RUNTIME_MARKER"
"""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_plugin_config_and_participant_paths_apply_to_actual_requests(baseline, tmp_path):
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        values = {
            "capability.tools": [{"name": "probe", "factory": "mechanism:make_tool"}],
            "action.hooks": [{"name": "configured", "factory": "hook:make"}],
            "capability.plugins": {"config": {"experimental-curator": {"note": "PLUGIN_CONFIG_RUNTIME_MARKER"}}},
            "memory.intake": "participant:Participant",
            "planning.advise": "participant:Participant",
            "capability.select_tools": "participant:Participant",
            "memory.archive": "participant:Participant",
        }
        candidate = inspection.declaration.accept(
            plan_for(*values),
            {
                "values": values,
                "files": {"mechanism.py": COMPOSED_CODE, "hook.py": HOOK_CODE, "participant.py": PARTICIPANT_CODE},
            },
        )
        await worker.install(candidate)
        result = await worker.run("Use the current mechanism.")
        requests = [row for row in result.records if row["kind"] == "provider.request"]
        assert "INTAKE_RUNTIME_MARKER" in str(requests[0])
        assert "ADVICE_RUNTIME_MARKER" in str(requests[0])
        assert "PLUGIN_CONFIG_RUNTIME_MARKER" in str(requests[0])
        assert {tool["function"]["name"] for tool in requests[0]["parameters"]["tools"]} == {"probe"}
        archived = [
            row for row in result.records if row["kind"] == "participant.result" and row["target"] == "memory.archive"
        ]
        assert archived and archived[0]["result"] == {"curator": {"archived": True}}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_salvage_is_reached_when_the_native_turn_has_no_answer(baseline, tmp_path):
    from functools import partial

    baseline.config.agents.defaults.max_tool_iterations = 1
    baseline.config.agents.defaults.empty_recovery_enabled = False
    from raven.contracts.llm_provider import ErrorClassification

    factory = partial(
        replay_provider,
        responses=tuple(
            LLMResponse(
                content="model failure",
                finish_reason="error",
                error_classification=ErrorClassification(category="unknown"),
            )
            for _ in range(8)
        ),
    )
    async with Worker(baseline, tmp_path / "runtime", provider_factory=factory, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("action.salvage"),
            {
                "values": {"action.salvage": "participant:Participant"},
                "files": {"participant.py": PARTICIPANT_CODE},
            },
        )
        await worker.install(candidate)
        result = await worker.run("Complete the task.")
        assert "SALVAGE_RUNTIME_MARKER" in str(result.events)
        assert any(row["kind"] == "participant.result" and row["target"] == "action.salvage" for row in result.records)


class MCPProvider(ReplayProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs):
        from raven.contracts.llm_provider import ToolCallRequest

        if not any(row.get("role") == "tool" for row in messages):
            name = next(tool["function"]["name"] for tool in tools if tool["function"]["name"].endswith("reflect"))
            return LLMResponse(content=None, tool_calls=[ToolCallRequest("mcp", name, {"value": "hello"})])
        return LLMResponse(content="done")


def mcp_provider(config):
    return MCPProvider([])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generated_mcp_server_connects_executes_and_closes(baseline, tmp_path):
    import asyncio
    import os
    import sys

    baseline.config.permissions.mode = "full"
    pidfile = tmp_path / "mcp.pid"
    server = """
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

Path(os.environ["CURATOR_TEST_PIDFILE"]).write_text(str(os.getpid()))
server = FastMCP("curated")

@server.tool()
def reflect(value: str) -> str:
    return "MCP_RUNTIME_MARKER:" + value

if __name__ == "__main__":
    server.run(transport="stdio")
"""
    async with Worker(baseline, tmp_path / "runtime", provider_factory=mcp_provider, timeout=40) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("capability.mcp"),
            {
                "values": {
                    "capability.mcp": {
                        "local": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["server.py"],
                            "env": {"CURATOR_TEST_PIDFILE": str(pidfile)},
                        }
                    }
                },
                "files": {"server.py": server},
            },
        )
        current = await worker.install(candidate)
        assert current.facts["mcp"][0]["connected"]
        result = await worker.run("Use the available reflection tool.")
        assert "MCP_RUNTIME_MARKER:hello" in str(result.events)
    pid = int(pidfile.read_text())
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the native MCP child outlived worker cleanup")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_construction_failure_is_returned_to_generation_as_evidence(baseline, tmp_path):
    code = "def make(context):\n    raise RuntimeError('FACTORY_FAILURE_MARKER')\n"
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("capability.tools"),
            {
                "values": {"capability.tools": [{"name": "probe", "factory": "broken:make"}]},
                "files": {"broken.py": code},
            },
        )
        report = await worker.check(candidate)
        assert not report.passed
        assert any(
            row["kind"] == "component.error" and "FACTORY_FAILURE_MARKER" in row["error"] for row in report.observations
        )
        result = await worker.run("The baseline remains usable.")
        assert not result.errors


class SlowProvider(ReplayProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs):
        import asyncio

        await asyncio.sleep(60)
        return LLMResponse(content="late")


def slow_provider(config):
    return SlowProvider([])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cancellation_ends_the_worker_and_keeps_partial_observations(baseline, tmp_path):
    import asyncio

    worker = Worker(baseline, tmp_path / "runtime", provider_factory=slow_provider, timeout=30)
    async with worker:
        pending = asyncio.create_task(worker.run("Wait for an answer."))
        deadline = asyncio.get_running_loop().time() + 8
        while not any(row["kind"] == "provider.request" for row in worker.records()):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert worker._process is None
        assert any(row["kind"] == "provider.request" for row in worker.records())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_validation_timeout_cannot_replace_the_active_harness(baseline, tmp_path):
    code = "def make(context):\n    while True:\n        pass\n"
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=3) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("capability.tools"),
            {
                "values": {"capability.tools": [{"name": "probe", "factory": "hanging:make"}]},
                "files": {"hanging.py": code},
            },
        )
        report = await worker.check(candidate)
        assert not report.passed and "timed out" in str(report.errors)
        assert any(row["kind"] == "component.call" for row in report.observations)
        assert not worker.artifact.values
        result = await worker.run("The original worker still answers.")
        assert not result.errors


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stale_candidates_and_no_change_installations_preserve_the_active_worker(baseline, tmp_path):
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        stale = inspection.declaration.accept(
            plan_for("memory.prompt"),
            {
                "values": {"memory.prompt": {"TOOLS.md": "stale"}},
            },
        )
        first = inspection.declaration.accept(
            plan_for("action.config"),
            {
                "values": {"action.config": {"temperature": 0.3}},
            },
        )
        current = await worker.install(first)
        with pytest.raises(ValueError, match="baseline"):
            await worker.install(stale)
        pid = worker._process.pid
        unchanged = current.declaration.accept(plan_for(), {"values": {}})
        after = await worker.install(unchanged)
        assert after.declaration.baseline == current.declaration.baseline
        assert worker._process.pid == pid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_generated_returns_are_visible_to_validation_before_activation(baseline, tmp_path):
    code = """
from raven.contracts.participant import AgentParticipant
class Participant(AgentParticipant):
    async def review(self, step):
        return {"verdict": "not-a-native-verdict"}
"""
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("action.review"),
            {
                "values": {"action.review": "invalid:Participant"},
                "files": {"invalid.py": code},
            },
        )
        report = await worker.check(candidate, probe=exercise_turn)
        assert not report.passed
        assert any(row["kind"] == "participant.error" for row in report.observations)
        assert not worker.artifact.values


@pytest.mark.integration
@pytest.mark.asyncio
async def test_skill_content_reaches_the_native_model_input(baseline, tmp_path):
    async with Worker(baseline, tmp_path / "runtime", provider_factory=replay_provider, timeout=30) as worker:
        inspection = await worker.inspect()
        candidate = inspection.declaration.accept(
            plan_for("planning.skills"),
            {
                "values": {
                    "planning.skills": {
                        "guide/SKILL.md": (
                            "---\nname: guide\ndescription: A required task guide\nalways: true\n---\nSKILL_INJECTION_RUNTIME_MARKER"
                        )
                    }
                },
            },
        )
        await worker.install(candidate)
        result = await worker.run("Follow the available task guidance.")
        assert any(
            "SKILL_INJECTION_RUNTIME_MARKER" in str(row) for row in result.records if row["kind"] == "provider.request"
        )
