"""The generated worker table, and the promise that the feature off changes nothing.

Every assertion about the off state is the same assertion: a turn that writes no
table takes the path it took before this module existed. That is what makes the
seam safe to widen, and it is checked on the two surfaces a table can move --
the tool array the model is shown, and the agent a dispatch resolves to.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import EngineWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_graph import parse_dag_spec
from raven.agent.subagent.dag_tool import _DispatchBackend, _WorkerBackend
from raven.agent.subagent.delegate import DelegateTable, Worker, current_delegate, delegate_scope, outbound_charter
from raven.agent.subagent.prompt_capabilities import AgentCapabilities
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.config.schema import PlaybookConfig
from raven.playbook import NodeSpec, PlaybookSpec, Triggers
from raven.playbook.agent_generator import WorkerTableGenerator, build_table, emit_tool, render_charter
from raven.playbook.agent_spec import AgentPlaybookSpec
from raven.providers.base import LLMProvider, LLMResponse


class _Stub(LLMProvider):
    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _loop(workspace: Path, harness: str = "default", *, enabled: bool = True) -> AgentLoop:
    return AgentLoop(
        provider=_Stub(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        engine=EngineWiring(playbook_config=PlaybookConfig(enabled=enabled, agentHarness=harness)),
    )


def _spawn_schema(loop: AgentLoop) -> dict:
    for definition in loop.tools.get_definitions():
        fn = definition.get("function", definition)
        if fn.get("name") == "spawn":
            return fn
    raise AssertionError("spawn is not on the tool array")


def _dag_schema(loop: AgentLoop) -> dict:
    for definition in loop.tools.get_definitions():
        fn = definition.get("function", definition)
        if fn.get("name") == "run_subagent_dag":
            return fn
    raise AssertionError("run_subagent_dag is not on the tool array")


def _table() -> DelegateTable:
    return DelegateTable(
        workers={
            "research-a": Worker("research-a", "Raven-Research", "only A's pricing", "BRIEF-A\n\n"),
            "research-b": Worker("research-b", "Raven-Research", "only B's pricing", ""),
        }
    )


# --------------------------------------------------------------------------- #
# Off changes nothing                                                           #
# --------------------------------------------------------------------------- #


def test_the_switch_alone_moves_nothing_the_model_sees(workspace) -> None:
    """``generate`` with no table written is byte-identical to ``default``.

    The config being on is not the change; a table being bound is. Asserted on
    the whole rendered function, not just the enum, because the description and
    the required list move with it.
    """
    off = _spawn_schema(_loop(workspace, "default"))
    on = _spawn_schema(_loop(workspace, "generate"))
    assert json.dumps(off, sort_keys=True) == json.dumps(on, sort_keys=True)


def test_no_scope_means_no_table(workspace) -> None:
    assert current_delegate() is None


def test_an_empty_table_is_the_same_as_none(workspace) -> None:
    """``delegate_scope`` of an empty table must not narrow the roster to
    nothing: an empty result is "the generator found no worker worth naming",
    and answering it with an empty enum would be an unsatisfiable schema."""
    loop = _loop(workspace, "generate")
    baseline = _spawn_schema(loop)
    with delegate_scope(DelegateTable(workers={})):
        assert json.dumps(_spawn_schema(loop), sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_the_table_is_gone_when_the_scope_closes(workspace) -> None:
    loop = _loop(workspace, "generate")
    baseline = _spawn_schema(loop)
    with delegate_scope(_table()):
        assert _spawn_schema(loop)["parameters"]["properties"]["subagent"]["enum"] == ["research-a", "research-b"]
    assert json.dumps(_spawn_schema(loop), sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_the_scope_restores_on_an_exception(workspace) -> None:
    """The binding rides a ``finally``, not a pair of statements: a turn that
    raises or is cancelled must not leave its table behind."""
    with pytest.raises(RuntimeError):
        with delegate_scope(_table()):
            raise RuntimeError("boom")
    assert current_delegate() is None


# --------------------------------------------------------------------------- #
# On: what the model is offered                                                 #
# --------------------------------------------------------------------------- #


def test_the_workers_replace_the_roster_in_the_enum(workspace) -> None:
    loop = _loop(workspace, "generate")
    with delegate_scope(_table()):
        prop = _spawn_schema(loop)["parameters"]["properties"]["subagent"]
    assert prop["enum"] == ["research-a", "research-b"]


def test_the_workers_replace_the_dag_roster_too(workspace) -> None:
    """A graph must dispatch through the same generated Harness as spawn."""
    loop = _loop(workspace, "generate")
    with delegate_scope(_table()):
        prop = _dag_schema(loop)["parameters"]["properties"]["nodes"]["items"]["properties"]["subagent"]
        description = _dag_schema(loop)["description"]

    assert prop["enum"] == ["research-a", "research-b"]
    assert "research-a: only A's pricing [runs on Raven-Research]" in description


@pytest.mark.asyncio
async def test_a_dag_worker_resolves_and_carries_its_charter(workspace, monkeypatch) -> None:
    """The DAG freezes both halves of a worker before a background run starts."""

    class _RecordingBackend:
        kind = "raven-loop"

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def run(self, task: str, **kwargs) -> str:
            self.calls.append((task, outbound_charter()))
            return "done"

    loop = _loop(workspace, "generate")
    tool = loop.tools.get("run_subagent_dag")
    backend = _RecordingBackend()
    resolved: list[str] = []

    def resolve(node):
        resolved.append(node.subagent)
        return backend

    monkeypatch.setattr(tool, "_resolve_node", resolve)
    payload = {"memory": {"systemPrompt": "only A"}}
    table = DelegateTable(
        workers={
            "research-a": Worker(
                "research-a",
                "Raven",
                "only A's pricing",
                "BRIEF-A\n\n",
                payload,
            )
        }
    )

    with delegate_scope(table):
        preflight = await tool._preflight(
            parse_dag_spec(
                {
                    "task_summary": "research A",
                    "nodes": [
                        {
                            "id": "research-a-node",
                            "subagent": "research-a",
                            "node_summary": "research A",
                            "prompt_template": "find pricing",
                        }
                    ],
                }
            )
        )

    # A background graph may outlive its caller. Both the real roster identity
    # and the worker's charter must already be frozen during preflight.
    assert resolved == ["Raven"]
    assert preflight.spec.nodes[0].subagent == "Raven"
    await preflight.backends["research-a-node"].run("find pricing")
    assert backend.calls and backend.calls[0][0].startswith("BRIEF-A\n\n")
    assert backend.calls[0][1] == payload


@pytest.mark.asyncio
async def test_worker_capabilities_follow_the_resolved_roster_agent(workspace, monkeypatch) -> None:
    """A generated label is resolved before the real agent's gates run."""
    tool = _loop(workspace, "generate").tools.get("run_subagent_dag")
    monkeypatch.setattr(
        tool,
        "_capability_map",
        lambda: {"Raven": AgentCapabilities(reads_local_files=False)},
    )
    table = DelegateTable(workers={"file-reader": Worker("file-reader", "Raven", "read a file", "")})

    with delegate_scope(table), pytest.raises(DagValidationError) as exc_info:
        await tool._preflight(
            parse_dag_spec(
                {
                    "task_summary": "read a local file",
                    "nodes": [
                        {
                            "id": "read",
                            "subagent": "file-reader",
                            "node_summary": "read the file",
                            "prompt_template": "inspect {{ ref_path:notes.md }}",
                        }
                    ],
                }
            )
        )

    assert "sub-agent 'Raven'" in str(exc_info.value)
    assert "no-local-files" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stored_playbook_dag_ignores_the_turn_worker_table(workspace, monkeypatch) -> None:
    """A stored graph runs as authored even when a worker label collides."""

    class _RecordingBackend:
        kind = "raven-loop"

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def run(self, task: str, **kwargs) -> str:
            self.calls.append((task, outbound_charter()))
            return "done"

    loop = _loop(workspace, "generate")
    runtime = loop._playbooks
    assert runtime is not None
    runtime._executor._background = False
    tool = runtime.dag_tool
    backend = _RecordingBackend()
    resolved: list[str] = []

    def resolve(node):
        resolved.append(node.subagent)
        return backend

    monkeypatch.setattr(tool, "_resolve_node", resolve)
    runtime.store.save(
        PlaybookSpec(
            name="stored-raven",
            description="Run one stored Raven step",
            task_summary="stored Raven step",
            mode="dag",
            confirm=False,
            triggers=Triggers(keywords=["stored raven"]),
            nodes=[
                NodeSpec(
                    id="step",
                    subagent="Raven",
                    node_summary="run the stored step",
                    prompt_template="stored task",
                )
            ],
        )
    )
    payload = {"memory": {"systemPrompt": "turn-only prompt"}}
    colliding_table = DelegateTable(
        workers={
            "Raven": Worker(
                "Raven",
                "Raven-Research",
                "turn-only worker",
                "TURN CHARTER\n\n",
                payload,
            )
        }
    )

    with delegate_scope(colliding_table):
        plan = await runtime.load("stored-raven")

    assert plan is not None and plan.kind == "dag"
    assert resolved == ["Raven"]
    assert backend.calls
    assert "stored task" in backend.calls[0][0]
    assert "TURN CHARTER" not in backend.calls[0][0]
    assert backend.calls[0][1] is None


@pytest.mark.asyncio
async def test_worker_backend_keeps_legacy_run_signatures_and_can_be_copied() -> None:
    """The wrapper consumes optional metadata unsupported by older backends."""

    class _LegacyBackend:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, task: str, *, task_id: str, workspace: str, executor: object) -> str:
            self.calls.append(task)
            return "done"

    wrapped = deepcopy(_WorkerBackend(_LegacyBackend(), charter="BRIEF\n\n"))
    dispatched = deepcopy(_DispatchBackend(wrapped))
    result = await dispatched.run(
        "task",
        task_id="n1",
        workspace="/tmp/work",
        executor=object(),
        authored_task="authored task",
    )

    assert result == "done"
    assert dispatched.backend.backend.calls == ["BRIEF\n\ntask"]


def test_each_worker_carries_its_brief_into_the_description(workspace) -> None:
    """The brief is why a label beats a bare agent name: the task the model
    writes has to match the charter, and it can only do that if it reads it."""
    loop = _loop(workspace, "generate")
    with delegate_scope(_table()):
        prop = _spawn_schema(loop)["parameters"]["properties"]["subagent"]
    assert "only A's pricing" in prop["description"]
    assert "only B's pricing" in prop["description"]
    assert "Raven-Research" in prop["description"]


def test_the_label_is_the_only_thing_that_reads_as_a_value(workspace) -> None:
    """The agent a worker runs on is shown, but never where the label goes.

    Measured on real turns rather than guessed at: rendered as
    ``- label (Agent): brief`` the dispatching model read the parenthesised
    agent as the value to pass and sent the roster name, which the enum then
    refused. One run, refused twice, abandoned delegation and did the work
    itself -- so this is the difference between a table that shapes a dispatch
    and one that stops it happening.
    """
    loop = _loop(workspace, "generate")
    with delegate_scope(_table()):
        prop = _spawn_schema(loop)["parameters"]["properties"]["subagent"]

    description = prop["description"]
    for label in prop["enum"]:
        line = next(ln for ln in description.splitlines() if ln.startswith(f"- {label}"))
        assert not line.startswith(f"- {label} (Raven-Research)"), (
            "an agent name directly after the label reads as the value to pass"
        )
        assert "Raven-Research" in line, "which agent runs it is still worth knowing"
    assert "not the agent it runs on" in description


def test_spawn_renders_live_so_a_table_can_reach_the_model(workspace) -> None:
    """``SpawnTool`` authors its own ``to_schema``, which is what makes the
    registry serve it live instead of from the admission snapshot. Without that
    the enum above would be frozen before any turn ran."""
    from raven.agent.subagent.spawn_tool import SpawnTool
    from raven.contracts.tool import Tool

    assert SpawnTool.to_schema is not Tool.to_schema


# --------------------------------------------------------------------------- #
# On: what a dispatch resolves to                                               #
# --------------------------------------------------------------------------- #


def test_a_label_resolves_to_the_roster_agent_behind_it(workspace) -> None:
    """Nothing downstream may see a label: it resolves to no backend, so the
    dispatch, the instance registry and the DAG tool are all given the agent."""
    tool = _loop(workspace, "generate").tools.get("spawn")
    with delegate_scope(_table()):
        agent, charter = tool._resolve_worker("research-a")
    assert agent == "Raven-Research"
    assert charter == "BRIEF-A\n\n"


def test_two_labels_may_share_one_agent(workspace) -> None:
    """The whole point of a label: one question, two researchers, two briefs,
    without registering an agent per pair."""
    tool = _loop(workspace, "generate").tools.get("spawn")
    with delegate_scope(_table()):
        assert tool._resolve_worker("research-a")[0] == tool._resolve_worker("research-b")[0] == "Raven-Research"
        assert tool._resolve_worker("research-a")[1] != tool._resolve_worker("research-b")[1]


def test_an_unknown_name_passes_through_untouched(workspace) -> None:
    """The roster check downstream is the one place that refuses a name;
    answering the same mistake here would word it twice."""
    tool = _loop(workspace, "generate").tools.get("spawn")
    with delegate_scope(_table()):
        assert tool._resolve_worker("Raven-PPT") == ("Raven-PPT", "")


def test_no_table_leaves_the_name_alone(workspace) -> None:
    tool = _loop(workspace, "default").tools.get("spawn")
    assert tool._resolve_worker("Raven-Research") == ("Raven-Research", "")


# --------------------------------------------------------------------------- #
# The charter                                                                   #
# --------------------------------------------------------------------------- #


def test_the_charter_ends_by_handing_over_to_the_task() -> None:
    """It is a preamble, not a replacement: the worker reads its brief and then
    the thing this dispatch actually asked for."""
    charter = render_charter("b", "only A", "both tables land", ["web_search"])
    assert charter.endswith("The task follows.\n\n")
    assert "only A" in charter
    assert "web_search" in charter
    assert "both tables land" in charter


def test_a_worker_with_nothing_to_say_gets_no_preamble() -> None:
    assert render_charter("", "", "", None) == ""


def test_the_brief_stands_in_when_no_prompt_was_written() -> None:
    assert "only A" in render_charter("only A", "", "", None)


# --------------------------------------------------------------------------- #
# Generation                                                                    #
# --------------------------------------------------------------------------- #


def test_the_roster_and_the_tools_are_enums_not_prose() -> None:
    """A name the host cannot resolve is refused at the boundary rather than
    diagnosed after, which is what keeps it out of the repair budget."""
    schema = emit_tool(["Raven-Research"], ["web_search"])[0]["function"]["parameters"]
    worker = schema["properties"]["workers"]["items"]["properties"]
    assert worker["name"]["enum"] == ["Raven-Research"]
    assert worker["tools"]["items"]["enum"] == ["web_search"]


def test_a_worker_off_the_roster_is_dropped_not_repaired() -> None:
    """The roster was an enum, so a name outside it is a shape the request
    could not express; spending a repair round on it teaches nothing."""
    spec = AgentPlaybookSpec.model_validate({"delegate": [{"as": "a", "name": "Raven-Research"}]})
    assert [entry.label for entry in spec.delegate] == ["a"]


def test_two_entries_may_not_share_a_label() -> None:
    with pytest.raises(Exception, match="duplicate worker label"):
        AgentPlaybookSpec.model_validate(
            {"delegate": [{"as": "x", "name": "Raven-Research"}, {"as": "x", "name": "Raven-PPT"}]}
        )


def test_a_label_defaults_to_the_agent_name() -> None:
    spec = AgentPlaybookSpec.model_validate({"delegate": [{"name": "Raven-PPT"}]})
    assert spec.delegate[0].label == "Raven-PPT"


def test_a_workers_playbook_may_not_bring_workers() -> None:
    """Depth is a property of what a charter may say, not a counter the runtime
    carries -- so it is checkable on the file alone."""
    with pytest.raises(Exception):
        AgentPlaybookSpec.model_validate(
            {"delegate": [{"name": "Raven-PPT", "playbook": {"delegate": [{"name": "Raven-Code"}]}}]}
        )


def test_an_empty_tool_list_survives_the_parse() -> None:
    """Three-valued: folding ``[]`` into unset would turn "this worker needs no
    tools" into "it gets all of them"."""
    spec = AgentPlaybookSpec.model_validate(
        {"delegate": [{"name": "Raven-PPT", "playbook": {"capability": {"tools": []}}}]}
    )
    assert spec.delegate[0].playbook.capability.tools == []


def test_generation_with_no_roster_writes_nothing() -> None:
    table = asyncio.run(WorkerTableGenerator(_Stub()).generate("do a thing", [], ["read_file"]))
    assert table is None


def test_generation_on_an_empty_question_writes_nothing() -> None:
    table = asyncio.run(WorkerTableGenerator(_Stub()).generate("   ", ["Raven-PPT"], ["read_file"]))
    assert table is None


def test_a_failed_model_call_leaves_the_turn_unconfigured() -> None:
    """A turn that dies because its setup step failed is strictly worse than a
    turn that runs without one."""

    class _Broken(_Stub):
        async def chat_with_retry(self, **kwargs):
            raise RuntimeError("provider down")

    table = asyncio.run(WorkerTableGenerator(_Broken()).generate("q", ["Raven-PPT"], ["read_file"]))
    assert table is None


def test_a_table_is_built_from_what_the_model_emitted() -> None:
    spec = AgentPlaybookSpec.model_validate(
        {
            "delegate": [
                {"as": "a", "name": "Raven-Research", "playbook": {"memory": {"systemPrompt": "only A"}}},
                {"as": "b", "name": "Raven-Research"},
            ]
        }
    )
    table = build_table(spec, {"a": "A's pricing", "b": "B's pricing"})
    assert table.labels() == ["a", "b"]
    assert table.get("a").agent == "Raven-Research"
    assert "only A" in table.get("a").charter
    assert table.get("b").brief == "B's pricing"


# --------------------------------------------------------------------------- #
# What the setup call must not cost                                            #
# --------------------------------------------------------------------------- #


class _CountingBinding:
    """A binding whose provider records every generation call made on it."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []
        self.model = "stub"
        self.provider = self

    def get_default_model(self) -> str:
        return "stub"

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs):
        from raven.providers.base import LLMResponse

        self.calls.append(self.label)
        return LLMResponse(content="no table", finish_reason="stop")


def _request(text: str = "do the thing", *, direct_target=None):
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
        conversation="test:c1",
        direct_target=direct_target,
    )


@pytest.mark.asyncio
async def test_a_direct_chat_pays_for_no_worker_table(workspace) -> None:
    """A direct chat returns through ``subagents.chat`` without ever rendering
    or executing ``spawn``, so a table written for it is never read. The cost
    of writing one is a model round trip, and two when it needs a repair."""
    loop = _loop(workspace, "generate")
    binding = _CountingBinding("generation")

    table = await loop._write_worker_table(_request(direct_target=("Raven-Research", "h1")), "s1", binding)

    assert table is None
    assert binding.calls == [], "a direct turn must not reach the generator at all"


@pytest.mark.asyncio
async def test_an_ordinary_turn_still_reaches_the_generator(workspace) -> None:
    """The guard above must be the direct path and nothing wider."""
    loop = _loop(workspace, "generate")
    binding = _CountingBinding("generation")

    await loop._write_worker_table(_request(), "s1", binding)

    # One call, plus the repair round this stub provokes by emitting no table --
    # which is the cost the guard above spares every direct chat.
    assert binding.calls == ["generation", "generation"]


@pytest.mark.asyncio
async def test_the_master_switch_prevents_worker_generation(workspace) -> None:
    """Enabled is the master switch even if agentHarness says generate."""
    loop = _loop(workspace, "generate", enabled=False)
    binding = _CountingBinding("generation")

    table = await loop._write_worker_table(_request(), "s1", binding)

    assert table is None
    assert binding.calls == []


@pytest.mark.asyncio
async def test_the_setup_call_runs_on_the_binding_it_was_handed(workspace) -> None:
    """One pair for the whole turn tree. The generation awaits a model call,
    and a session that switched while it was in flight would otherwise have the
    setup run on the old pair and the turn body on the new one -- which is what
    the Model Binding contract forbids. Resolving it once, outside, is what
    makes that impossible rather than unlikely."""
    loop = _loop(workspace, "generate")
    captured = _CountingBinding("captured")
    loop.set_default_binding(_CountingBinding("switched-to"))

    await loop._write_worker_table(_request(), "s1", captured)

    assert set(captured.calls) == {"captured"}, "the generator used a pair nobody handed it"
    assert captured.calls, "the generator was never reached"


def test_the_roster_reaches_the_generator_with_what_each_agent_is_for(workspace) -> None:
    """An enum of names is only selectable when the names say what they are.
    The shipped roster reads that way; a deployment's own does not."""
    from raven.playbook.agent_generator import emit_tool

    schema = emit_tool(["alpha", "beta"], ["grep"], {"alpha": "owns legal research", "beta": "owns code review"})
    described = schema[0]["function"]["parameters"]["properties"]["workers"]["items"]["properties"]["name"]

    assert described["enum"] == ["alpha", "beta"]
    assert "owns legal research" in described["description"]
    assert "owns code review" in described["description"]


def test_a_roster_that_says_nothing_still_renders(workspace) -> None:
    from raven.playbook.agent_generator import emit_tool

    described = emit_tool(["alpha"], ["grep"])[0]["function"]["parameters"]["properties"]["workers"]["items"][
        "properties"
    ]["name"]
    assert described["description"] == "Which sub-agent this worker is."


class _Meta:
    """A roster row with only what selection reads."""

    def __init__(self, name: str, **kw) -> None:
        self.name = name
        self.owns = ""
        self.description = ""
        self.stateful = False
        self.reads_local_files = False
        self.live_progress = False
        self.owns_watched_work = False
        self.__dict__.update(kw)


def test_two_agents_with_blank_descriptions_are_still_told_apart(workspace) -> None:
    """Prose is not the only thing a choice turns on, and a roster may carry
    none. Two rows that describe themselves identically -- with nothing -- are
    not interchangeable if only one of them can read the local files the task
    is about, and a generator shown only names can drop the one that can.
    ``spawn`` gates on these same capabilities, which is why they are shown.
    """
    from raven.playbook.agent_generator import emit_tool, roster_note

    metas = [_Meta("alpha"), _Meta("beta", stateful=True, reads_local_files=True, live_progress=True)]
    notes = {m.name: roster_note(m) for m in metas}

    described = emit_tool(["alpha", "beta"], ["grep"], notes)[0]["function"]["parameters"]["properties"]["workers"][
        "items"
    ]["properties"]["name"]["description"]

    assert "beta: reads local files" in described
    assert "resumable across dispatches" in described
    assert "reports progress while it runs" in described


def test_prose_and_capabilities_ride_together_when_a_row_has_both(workspace) -> None:
    """The shipped roster has both, and the sharper of the two leads."""
    from raven.playbook.agent_generator import roster_note

    note = roster_note(_Meta("Raven-Research", owns="research: the live web read.", reads_local_files=True))

    assert note == "research: the live web read (reads local files)"
