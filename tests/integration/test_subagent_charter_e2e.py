"""A charter, end to end: written by a host, carried on a dispatch, held to by a turn.

Each unit of this is tested beside its own code. What is only true of the whole
is that the pieces are actually joined -- that a brief written on one side
reaches a tool call on the other, and that a turn carrying none is the turn it
was before charters existed. Those joins are what this file holds:

- the host's generated table produces a payload, and the transport asks for it;
- the worker's ACP entry stages it, and the turn consumes it once;
- the assembled prompt carries the brief, and the tool registry refuses on it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import ToolWiring, TurnPolicy
from raven.agent.subagent.charter import (
    Charter,
    charter_scope,
    current_charter,
    narrowed_timeout,
)
from raven.agent.subagent.delegate import dispatch_charter, outbound_charter
from raven.playbook.agent_generator import build_table
from raven.playbook.agent_spec import AgentPlaybookSpec
from raven.providers.base import LLMProvider, LLMResponse

BRIEF = "Only look at A. Leave B alone."
JUDGE = """
def judge(name, params, prior):
    if name != "write_file":
        return []
    if not str(params.get("path", "")).startswith("./out/a/"):
        return ["write under ./out/a/ only"]
    return []
"""


class _Stub(LLMProvider):
    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _loop(workspace: Path) -> AgentLoop:
    return AgentLoop(
        provider=_Stub(),
        workspace=workspace,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )


def _spec(**over) -> AgentPlaybookSpec:
    playbook = {
        "role": "subagent",
        "memory": {"systemPrompt": BRIEF},
        "capability": {"tools": ["web_search", "web_fetch"]},
        "stopWhen": "both tables land",
    }
    playbook.update(over)
    return AgentPlaybookSpec.model_validate(
        {"delegate": [{"as": "research-a", "name": "Raven-Research", "playbook": playbook}]}
    )


# --------------------------------------------------------------------------- #
# Host: a table becomes something a dispatch can carry                          #
# --------------------------------------------------------------------------- #


def test_the_generated_table_carries_a_payload_the_worker_can_read() -> None:
    worker = build_table(_spec(), {"research-a": "only A's pricing"}).get("research-a")
    assert worker.payload["prompt"] == BRIEF
    assert worker.payload["tools"] == ["web_search", "web_fetch"]
    assert worker.payload["stopWhen"] == "both tables land"


def test_a_worker_with_no_brief_carries_nothing() -> None:
    """No payload means no key on the wire, which is what keeps a dispatch
    without a playbook byte-identical to one from before charters existed."""
    table = build_table(AgentPlaybookSpec.model_validate({"delegate": [{"name": "Raven-PPT"}]}), {})
    assert table.get("Raven-PPT").payload is None


def test_the_transport_is_offered_the_charter_only_inside_a_dispatch() -> None:
    worker = build_table(_spec(), {"research-a": "b"}).get("research-a")
    assert outbound_charter() is None
    with dispatch_charter(worker.payload):
        assert outbound_charter() == worker.payload
    assert outbound_charter() is None


def test_the_payload_survives_the_round_trip_to_a_charter() -> None:
    """The shape the host writes and the shape the worker reads are one
    vocabulary; a field either side invented alone would be dropped here."""
    from raven.agent.subagent.charter import parse

    worker = build_table(_spec(), {"research-a": "b"}).get("research-a")
    charter = parse(worker.payload)
    assert charter is not None
    assert charter.prompt == BRIEF
    assert charter.tools == ("web_search", "web_fetch")
    assert charter.stop_when == "both tables land"


# --------------------------------------------------------------------------- #
# Worker: staged on arrival, consumed by the turn                               #
# --------------------------------------------------------------------------- #


def test_a_staged_charter_is_taken_by_the_turn(workspace) -> None:
    loop = _loop(workspace)
    loop.bind_session_charter("s1", {"prompt": BRIEF, "tools": ["grep"]})
    taken = loop._take_session_charter("s1")
    assert taken is not None
    assert taken.prompt == BRIEF


def test_a_charter_is_taken_once_and_not_again(workspace) -> None:
    """It describes one dispatch. Left behind, it would hold the next turn of a
    resumable instance to a brief written for the last one."""
    loop = _loop(workspace)
    loop.bind_session_charter("s1", {"prompt": BRIEF})
    assert loop._take_session_charter("s1") is not None
    assert loop._take_session_charter("s1") is None


def test_one_session_s_charter_is_not_another_s(workspace) -> None:
    """One process serves every session on a connection."""
    loop = _loop(workspace)
    loop.bind_session_charter("s1", {"prompt": BRIEF})
    assert loop._take_session_charter("s2") is None
    assert loop._take_session_charter("s1") is not None


def test_staging_nothing_clears_rather_than_keeps(workspace) -> None:
    loop = _loop(workspace)
    loop.bind_session_charter("s1", {"prompt": BRIEF})
    loop.bind_session_charter("s1", None)
    assert loop._take_session_charter("s1") is None


def test_a_turn_with_no_charter_staged_takes_none(workspace) -> None:
    assert _loop(workspace)._take_session_charter("never-seen") is None


# --------------------------------------------------------------------------- #
# The turn: what the charter actually reaches                                   #
# --------------------------------------------------------------------------- #


def test_the_brief_reaches_the_assembled_identity(workspace) -> None:
    """Not the helper in isolation: the text a real identity segment renders."""
    from raven.context_engine.segments.identity import IdentitySegmentBuilder

    segment = IdentitySegmentBuilder(workspace=workspace)
    import asyncio

    plain = asyncio.run(segment.build(None)).text
    with charter_scope(Charter(prompt=BRIEF, stop_when="both tables land")):
        briefed = asyncio.run(segment.build(None)).text

    assert BRIEF not in plain
    assert BRIEF in briefed
    assert "both tables land" in briefed
    assert briefed.startswith(plain), "the brief is appended; the runtime facts above it stay"


def test_the_tool_array_is_narrowed_for_a_briefed_turn(workspace) -> None:
    loop = _loop(workspace)
    offered = {d.get("function", d).get("name") for d in loop.tools.get_definitions()}
    assert {"read_file", "grep"} <= offered

    with charter_scope(Charter(tools=("grep",))):
        narrowed = {d.get("function", d).get("name") for d in loop.tools.get_definitions()}
    assert narrowed == {"grep"}

    assert {d.get("function", d).get("name") for d in loop.tools.get_definitions()} == offered


@pytest.mark.asyncio
async def test_a_call_the_charter_forbids_is_refused_by_the_registry(workspace) -> None:
    """The join that matters most: a rule returning a sentence is not the same
    as a tool call actually being stopped."""
    from raven.agent.subagent.charter import CheckRule

    loop = _loop(workspace)
    charter = Charter(checks=(CheckRule(tool="write_file", path_prefix="./out/", message="write under ./out/"),))
    with charter_scope(charter):
        refused = await loop.tools.execute("write_file", {"path": "escape.txt", "content": "x"})
    assert "write under ./out/" in str(refused)
    assert not (workspace / "escape.txt").exists(), "a refused call must not have run"


@pytest.mark.asyncio
async def test_the_same_call_runs_when_the_charter_allows_it(workspace) -> None:
    from raven.agent.subagent.charter import CheckRule

    loop = _loop(workspace)
    (workspace / "out").mkdir()
    charter = Charter(checks=(CheckRule(tool="write_file", path_prefix="out/", message="write under out/"),))
    with charter_scope(charter):
        await loop.tools.execute("write_file", {"path": "out/kept.txt", "content": "x"})
    assert (workspace / "out" / "kept.txt").exists()


@pytest.mark.asyncio
async def test_a_charters_own_judge_can_refuse_a_call(workspace) -> None:
    """Code, not a rule, and reached the same way -- through the registry."""
    loop = _loop(workspace)
    with charter_scope(Charter(code=JUDGE)):
        refused = await loop.tools.execute("write_file", {"path": "elsewhere.txt", "content": "x"})
    assert "write under ./out/a/ only" in str(refused)
    assert not (workspace / "elsewhere.txt").exists()


@pytest.mark.asyncio
async def test_a_turn_without_a_charter_is_refused_nothing(workspace) -> None:
    loop = _loop(workspace)
    await loop.tools.execute("write_file", {"path": "free.txt", "content": "x"})
    assert (workspace / "free.txt").exists()


@pytest.mark.asyncio
async def test_a_successful_call_is_remembered_for_the_next_rule(workspace) -> None:
    """`requiresPrior` is only expressible because the registry records what
    ran. Recorded after dispatch and only on success, which is what stops a
    failed read from satisfying "read it first"."""
    from raven.agent.subagent.charter import CheckRule, prior_calls

    loop = _loop(workspace)
    (workspace / "seed.txt").write_text("hello")
    charter = Charter(
        checks=(
            CheckRule(
                tool="write_file",
                requires_prior="read_file",
                match_param="path",
                message="read it first",
            ),
        )
    )
    with charter_scope(charter):
        refused = await loop.tools.execute("write_file", {"path": "seed.txt", "content": "x"})
        assert "read it first" in str(refused)

        await loop.tools.execute("read_file", {"path": "seed.txt"})
        assert any(name == "read_file" for name, _ in prior_calls())

        await loop.tools.execute("write_file", {"path": "seed.txt", "content": "x"})
    assert (workspace / "seed.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_a_read_that_failed_does_not_satisfy_read_it_first(workspace) -> None:
    """The whole point of recording after the verdict: a read that errored read
    nothing, and letting it count would hand the write the permission the rule
    exists to withhold."""
    from raven.agent.subagent.charter import CheckRule

    loop = _loop(workspace)
    charter = Charter(
        checks=(
            CheckRule(
                tool="write_file",
                requires_prior="read_file",
                match_param="path",
                message="read it first",
            ),
        )
    )
    with charter_scope(charter):
        await loop.tools.execute("read_file", {"path": "does-not-exist.txt"})
        refused = await loop.tools.execute("write_file", {"path": "does-not-exist.txt", "content": "x"})
    assert "read it first" in str(refused)


# --------------------------------------------------------------------------- #
# The deadline                                                                  #
# --------------------------------------------------------------------------- #


def test_a_brief_tightens_the_dispatch_deadline() -> None:
    from raven.acp_client.acp_agent import _prompt_deadline

    assert _prompt_deadline(None) is None
    with charter_scope(Charter(timeout_s=60)):
        assert _prompt_deadline(None) == 60
        assert _prompt_deadline(600) == 60
        assert _prompt_deadline(30) == 30


def test_the_wire_carries_no_charter_key_without_one() -> None:
    """A dispatch with no brief puts exactly the bytes on the wire it put
    before charters existed."""
    from raven.acp_client.acp_agent import _prompt_meta

    assert set(_prompt_meta(None)) == {"raven.usage"}
    with dispatch_charter({"prompt": BRIEF}):
        assert set(_prompt_meta(None)) == {"raven.usage", "raven.playbook"}


def test_leaving_the_scope_leaves_no_charter_behind() -> None:
    with charter_scope(Charter(prompt=BRIEF)):
        assert current_charter() is not None
    assert current_charter() is None
    assert narrowed_timeout(600) == 600


@pytest.mark.asyncio
async def test_a_tool_that_reported_failure_without_the_word_error_is_not_remembered() -> None:
    """The verdict is read off the result, not off its text.

    ``run_shell`` is the case that made this concrete: a command that exits
    non-zero says so through ``ok`` and prints whatever it prints, which rarely
    begins with the word Error. Judged by its text alone it reads as a success,
    and a ``requiresPrior`` rule would then be satisfied by a call that did not
    do the thing the rule is about.
    """
    from raven.agent.subagent.charter import prior_calls
    from raven.agent.tools.registry import ToolRegistry
    from raven.contracts.tool import Tool, ToolOutput

    class _Exited(Tool):
        @property
        def name(self) -> str:
            return "shellish"

        @property
        def description(self) -> str:
            return "fails the way a shell fails"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}, "required": []}

        async def execute(self, **kwargs) -> ToolOutput:
            return ToolOutput("bash: no such file or directory", ok=False)

    registry = ToolRegistry()
    registry.register(_Exited())
    with charter_scope(Charter(prompt=BRIEF)):
        await registry.execute("shellish", {})
        assert prior_calls() == (), "a call that reported ok=False is not a prior call"


def test_a_checks_field_that_is_not_a_list_costs_a_log_line_not_the_dispatch() -> None:
    """``parse`` reads a payload another process wrote, inside the request
    handler that accepts the dispatch. A sender that put a single rule object
    where a list belongs must not be able to raise in there."""
    from raven.agent.subagent.charter import parse

    for malformed in (5, "checks", {"tool": "write_file"}, True):
        charter = parse({"prompt": BRIEF, "checks": malformed})
        assert charter is not None
        assert charter.checks == ()
        assert charter.prompt == BRIEF


@pytest.mark.asyncio
async def test_the_charter_survives_into_the_background_task_the_dispatch_runs_on() -> None:
    """``SubagentManager.spawn`` does not send the prompt itself: it creates a
    task and returns, so the ``with`` closes long before the transport asks for
    the charter. What carries it across is that ``asyncio.create_task`` copies
    the context at creation -- a property worth a test of its own, because the
    scope-based design is wrong the moment it stops holding.
    """
    import asyncio

    from raven.acp_client.acp_agent import _prompt_meta

    seen: dict[str, object] = {}
    started = asyncio.Event()

    async def _later() -> None:
        await started.wait()  # the scope is already closed by now
        seen["meta"] = set(_prompt_meta(None))
        seen["charter"] = outbound_charter()

    with dispatch_charter({"prompt": BRIEF}):
        task = asyncio.create_task(_later())

    assert outbound_charter() is None, "the dispatching turn is out of the scope"
    started.set()
    await task

    assert seen["charter"] == {"prompt": BRIEF}
    assert seen["meta"] == {"raven.usage", "raven.playbook"}


@pytest.mark.asyncio
async def test_two_dispatches_in_flight_do_not_read_each_other_s_charter() -> None:
    """One turn spawning two workers is the case the table exists for."""
    import asyncio

    async def _dispatch(brief: str) -> object:
        with dispatch_charter({"prompt": brief}):
            task = asyncio.create_task(asyncio.sleep(0, result=None))
            await task
            return outbound_charter()

    a, b = await asyncio.gather(_dispatch("only A"), _dispatch("only B"))
    assert a == {"prompt": "only A"}
    assert b == {"prompt": "only B"}
    assert outbound_charter() is None


@pytest.mark.asyncio
async def test_a_sub_agent_that_runs_in_this_process_is_held_to_its_charter(workspace) -> None:
    """Not every sub-agent is a fork. ``kind: builtin`` runs the loop right
    here, with no wire between the dispatch and the run, so the ``_meta`` that
    carries a charter to a forked worker has nothing to travel on. Without this
    the brief would ride along in the task text while the tools, the checks and
    the deadline it named were dropped without a word.
    """
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    seen: dict[str, object] = {}

    backend = RavenLoopBackend(provider=_Stub(), model="stub", agent_home=workspace)

    async def _capture(*args, **kwargs) -> str:
        charter = current_charter()
        seen["prompt"] = charter.prompt if charter else None
        seen["tools"] = charter.tools if charter else None
        return "done"

    backend._run = _capture  # type: ignore[method-assign]

    with dispatch_charter({"prompt": BRIEF, "tools": ["grep"]}):
        await backend.run("do the thing", task_id="t1", workspace=workspace, executor=None)

    assert seen["prompt"] == BRIEF
    assert seen["tools"] == ("grep",)
    assert current_charter() is None, "the scope closes with the run"


@pytest.mark.asyncio
async def test_an_in_process_sub_agent_with_no_charter_runs_unbriefed(workspace) -> None:
    from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

    seen: list[object] = []
    backend = RavenLoopBackend(provider=_Stub(), model="stub", agent_home=workspace)

    async def _capture(*args, **kwargs) -> str:
        seen.append(current_charter())
        return "done"

    backend._run = _capture  # type: ignore[method-assign]
    await backend.run("do the thing", task_id="t1", workspace=workspace, executor=None)
    assert seen == [None]


def test_every_field_a_playbook_can_write_reaches_the_worker() -> None:
    """One case over the whole vocabulary, because the halves were written
    apart. The worker could read ``code`` and ``timeoutSeconds`` for a while
    before anything on the host could write them -- each half tested alone,
    and the join tested by nothing.
    """
    from raven.agent.subagent.charter import judge, narrowed_timeout, narrowed_tools, parse

    source = "def judge(name, params, prior):\n    return ['no shouting'] if 'URGENT' in str(params.get('content','')) else []\n"
    spec = AgentPlaybookSpec.model_validate(
        {
            "delegate": [
                {
                    "as": "w",
                    "name": "Raven",
                    "playbook": {
                        "role": "subagent",
                        "memory": {"systemPrompt": BRIEF},
                        "capability": {"tools": ["grep", "write_file"]},
                        "action": {
                            "checks": {
                                "rules": [{"tool": "write_file", "pathPrefix": "out/", "message": "under out/ only"}],
                                "code": source,
                            }
                        },
                        "stopWhen": "both tables land",
                        "timeoutSeconds": 90,
                    },
                }
            ]
        }
    )

    charter = parse(build_table(spec, {"w": "b"}).get("w").payload)
    assert charter is not None

    with charter_scope(charter):
        assert charter.prompt == BRIEF
        assert charter.stop_when == "both tables land"
        assert narrowed_tools(["grep", "write_file", "exec"]) == frozenset({"exec"})
        assert judge("write_file", {"path": "elsewhere", "content": "x"}, ()) == ["under out/ only"]
        assert judge("write_file", {"path": "out/a", "content": "URGENT"}, ()) == ["no shouting"]
        assert judge("write_file", {"path": "out/a", "content": "calm"}, ()) == []
        assert narrowed_timeout(None) == 90
        assert narrowed_timeout(30) == 30


def test_nothing_the_model_may_emit_is_quietly_dropped() -> None:
    """Every property of the emitted worker schema has a reader.

    The failure this catches is silent by construction: a field offered to the
    generating model and consumed by nobody produces a table that validates,
    a dispatch that runs, and a brief that is missing half of what its author
    wrote -- with no error anywhere. ``code`` and ``timeoutSeconds`` were in
    exactly that state.
    """
    from raven.playbook.agent_generator import _spec_from_args, emit_tool

    schema = emit_tool(["Raven"], ["grep", "write_file"])[0]["function"]["parameters"]
    offered = set(schema["properties"]["workers"]["items"]["properties"])

    row: dict = {
        "name": "Raven",
        "as": "w",
        "brief": "a brief",
        "systemPrompt": BRIEF,
        "stopWhen": "it lands",
        "tools": ["grep"],
        "checks": [{"tool": "write_file", "pathPrefix": "out/"}],
        "code": "def judge(name, params, prior):\n    return []\n",
        "timeoutSeconds": 90,
    }
    assert offered == set(row), "this test must exercise exactly what the schema offers"

    spec, briefs = _spec_from_args({"workers": [row]}, {"Raven"})
    payload = build_table(spec, briefs).get("w").payload

    assert briefs["w"] == "a brief"
    assert set(payload) == {"prompt", "tools", "stopWhen", "checks", "code", "timeoutSeconds"}
