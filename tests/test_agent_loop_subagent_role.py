"""Orchestration tools are withheld from a loop the host launched as a sub-agent.

``RavenLoopBackend`` builds a sub-agent's registry from an empty ``ToolRegistry``
and never adds ``spawn`` or ``run_subagent_dag``, so an in-process sub-agent
cannot delegate. A ``kind: acp`` agent whose command is ``raven acp`` gets none of
that: the child is a separate process building its own full registry, and it
delegated a DAG node's whole task to a background ``spawn`` whose receipt returns
before any work is done -- twice, ending both turns with a promise instead of the
briefing the node was asked for.

The signal is an environment variable the host injects into every child it
launches, alongside the ``RAVEN_HOME`` it already sends. What the two paths
withhold is one shared set, asserted equal here so neither can drift.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.agent.subagent.role import WITHHELD_FROM_SUBAGENT
from raven.agent.tools.create_playbook import CreatePlaybookTool
from raven.agent.tools.load_playbook import LoadPlaybookTool
from raven.agent.tools.registry import ToolRegistry, absent_tool_error
from raven.config.schema import PlaybookConfig
from raven.context_engine.segments import render
from raven.context_engine.segments.render import live_dispatch_tools
from raven.memory_engine import LocalSkillCatalog, filter_by_required_tools
from raven.providers.base import LLMProvider, LLMResponse
from tests._wiring import wire


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="stub", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True)
def _no_ambient_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer whose shell exports the variable would otherwise see the
    baseline half of every test below pass for the wrong reason."""
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)


def _loop(workspace: Path, **kw) -> AgentLoop:
    return AgentLoop(provider=_StubProvider(), workspace=workspace, model="stub", **wire(**kw))


def _equipped_loop(workspace: Path) -> AgentLoop:
    """A loop holding every withheld name, so the gate has something to remove.

    The two playbook tools arrive as plugin tools and unregister themselves when
    the loop builds no funnel, so a bare loop never holds them and an assertion
    that they are absent would pass over a loop that could not have had them.
    """
    return _loop(
        workspace,
        playbook_config=PlaybookConfig(enabled=True),
        plugin_tools=[LoadPlaybookTool(), CreatePlaybookTool()],
    )


def test_subagent_mode_withholds_spawn(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    # Unregistered rather than schema-hidden: ``_visible`` reads ``_tools``, so a
    # hidden tool is still reachable through ``tool_call`` -- which is how
    # ``resolve_dag_node`` is reached today.
    ordinary = _loop(workspace)
    assert ordinary.tools.has("spawn"), "baseline: an ordinary loop must hold spawn, or the gate proves nothing"

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = _loop(workspace)

    assert not delegated.tools.has("spawn")


def test_every_withheld_name_is_one_an_ordinary_loop_holds(workspace) -> None:
    """The constant names live tools, and the gate below is not asserting over
    names nothing ever registers."""
    ordinary = _equipped_loop(workspace)

    missing = sorted(name for name in WITHHELD_FROM_SUBAGENT if not ordinary.tools.has(name))

    assert not missing, f"{missing} is withheld from sub-agents but no ordinary loop registers it"


def test_subagent_mode_withholds_all_of_them(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")

    delegated = _equipped_loop(workspace)

    still_held = sorted(name for name in WITHHELD_FROM_SUBAGENT if delegated.tools.has(name))
    assert not still_held
    assert delegated.tools.has("read_file"), "a sub-agent still has to do the work it was handed"
    assert delegated.tools.has("message"), "message is deliberately not withheld over acp"


async def test_tool_call_cannot_reach_a_withheld_tool(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the gate unregisters rather than hides.

    ``hide_from_schema`` keeps a tool in ``_tools``, which is what ``_visible``
    reads, so a hidden tool is still reachable by name -- that is how
    ``resolve_dag_node`` is reached today, and it would leave the gate open.
    """
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = _equipped_loop(workspace)
    forwarder = delegated.tools.get("tool_call")
    assert forwarder is not None, "baseline: tool_call must exist, or nothing was tested"

    answer = await forwarder.execute(name="run_subagent_dag", arguments={})

    assert answer.startswith(absent_tool_error("run_subagent_dag")[:20])


def test_the_delegation_instruction_goes_with_the_tools(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resident prompt must not keep telling a sub-agent to hand work over.

    ``## Delegation`` is the one segment that *prohibits* work, and
    ``_delegation_block`` already drops it when no dispatch path is live -- a
    prohibition with no means of obeying it leaves the model choosing between
    doing the forbidden thing and abandoning the task. That guard reads the
    turn's real tool definitions, so the gate above is what empties it; asserted
    here because the two live in different packages and nothing else connects
    them.
    """
    ordinary = _loop(workspace)
    assert live_dispatch_tools(ordinary.tools.get_definitions), "baseline: an ordinary loop advertises a dispatch path"

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = _loop(workspace)

    assert live_dispatch_tools(delegated.tools.get_definitions) == ()


def test_the_prompt_tells_a_sub_agent_what_it_is(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """Withholding the tools stops the delegating; it does not tell the child what
    it is. An acp child otherwise renders the ordinary identity, which says nothing
    about having been launched to work for another raven -- so it answers as an
    agent that can still hand work on, and ends turns accordingly.

    What the section may assert is bounded by the lane it cannot see; the test
    below this one holds that boundary.
    """
    plain = render.identity_text(workspace)
    assert "## Sub-agent" not in plain

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = render.identity_text(workspace)

    assert "## Sub-agent" in delegated
    assert delegated.index("## Sub-agent") < delegated.index("## Raven Guidelines"), (
        "a rule about what the whole turn is for has to be read before the guidelines it qualifies"
    )


def test_the_orchestration_guide_goes_with_the_tools(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The DAG guide is an ``always`` skill, so it is resident unconditionally --
    and it declares ``requires.tools: [run_subagent_dag]``.

    ``ActiveSkillsSegmentBuilder._require_tools`` already drops an always-skill
    whose tools are not registered, reading the turn's live definitions, so the
    gate above is what empties it. Measured rather than assumed: the filter
    stands one package away and fails open when it cannot see a tool table.
    """
    catalog = LocalSkillCatalog(workspace)
    always = catalog.get_always_skills()
    assert always, "baseline: the shipped catalog must carry an always-skill, or this asserts nothing"

    ordinary = _loop(workspace)
    kept = filter_by_required_tools(always, render.collect_tool_names(ordinary.tools.get_definitions))
    assert kept, "baseline: an ordinary loop keeps the guide"

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = _loop(workspace)

    withheld = filter_by_required_tools(always, render.collect_tool_names(delegated.tools.get_definitions))
    assert withheld == []


def test_the_sub_agent_note_claims_nothing_about_who_is_reading(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """One pooled acp connection serves every call to an agent, so the same process
    answers a dispatched DAG node and a person's direct chat.

    ``acp_client/pool.py`` keys a connection on the launch config, and the role is
    applied at launch, so it cannot tell the two apart. A note asserting that the
    reply goes to an agent rather than a person, or that no turn follows, is
    therefore false on the direct-chat lane -- where the reply streams to the person
    and the instance stays available for the next turn. The note may claim what this
    process holds and what happens when its turn ends; those hold either way.
    """
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    note = render.identity_text(workspace)
    assert "## Sub-agent" in note, "baseline: the section must be present, or this asserts nothing"

    section = note.split("## Sub-agent", 1)[1].split("\n##", 1)[0].lower()

    for lane_dependent in ("not shown to a person", "nothing follows this turn", "is waiting on your"):
        assert lane_dependent not in section, (
            f"the note claims {lane_dependent!r}, which is false when a person is in a direct chat with this agent"
        )


def test_the_sub_agent_note_leaves_room_for_scheduled_delivery(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A flat "nothing runs after your turn ends" is false in an acp process.

    ``WiringMixin`` registers ``cron`` whenever a cron service exists, and
    ``build_rpc_stack`` starts one for this channel with ``make_on_session_wake``
    so a fired job runs later on the session that armed it. Telling the model
    otherwise would call its own scheduled reminder worthless. The qualifier is
    what the section turns on, so the qualifier is what is asserted -- banning a
    phrasing would pass over the next absolute sentence written a different way.
    """
    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    note = render.identity_text(workspace)
    section = note.split("## Sub-agent", 1)[1].split("\n##", 1)[0].lower()

    about_the_end = [line for line in section.splitlines() if "your turn ends" in line]
    assert about_the_end, "baseline: the section must say something about what follows the turn"

    assert all("unless" in line for line in about_the_end), (
        "an unqualified claim that nothing runs after the turn contradicts cron, which this "
        "process holds and which does deliver later"
    )


def test_the_ordinary_prompt_is_byte_identical_without_the_role(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The joint, not just the section: the delegation block above this one once
    rendered a prompt one blank line short on every install without a specialist,
    and a test comparing the function against itself agreed with it."""
    monkeypatch.delenv("RAVEN_SUBAGENT", raising=False)

    plain = render.identity_text(workspace)

    assert "\n\n\n## Raven Guidelines" in plain


def test_both_sub_agent_paths_withhold_the_same_tools(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The drift lock between the in-process backend and an acp child.

    ``RavenLoopBackend`` withholds by never registering into a registry it builds
    itself; the acp child withholds by the gate this module adds. Neither reads
    the other, so what they withhold is asserted equal rather than assumed.

    Each side is measured as what it ends up *holding*, which is the thing a model
    can reach. The two are observed differently only because the backend keeps no
    reference to the registry it builds inside ``run``: a ``register`` spy is the
    only way to see it. Reading the spy as the acp side's answer too would be
    wrong -- the playbook tools are registered as plugin tools and unregister
    themselves when they decline, so they appear in a registration log they are
    absent from the registry for.
    """
    registered: list[str] = []
    real = ToolRegistry.register

    def _spy(self, tool):  # noqa: ANN001, ANN202
        real(self, tool)
        registered.append(tool.name)

    monkeypatch.setattr(ToolRegistry, "register", _spy)

    async def _run_builtin() -> None:
        backend = RavenLoopBackend(provider=_StubProvider(), model="stub", agent_home=workspace / "home")
        await backend.run("task", task_id="t1", workspace=workspace, executor=None)

    import asyncio

    asyncio.run(_run_builtin())
    builtin = set(registered)

    monkeypatch.setenv("RAVEN_SUBAGENT", "1")
    delegated = _equipped_loop(workspace)

    assert "read_file" in builtin and delegated.tools.has("read_file"), "baseline: neither side may be empty"
    over_acp = {name for name in WITHHELD_FROM_SUBAGENT if delegated.tools.has(name)}
    assert WITHHELD_FROM_SUBAGENT & builtin == over_acp == set()
