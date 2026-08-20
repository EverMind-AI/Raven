"""The field definition's rule table: graph coherence, references, instances."""

from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.validate import (
    check_assets,
    validate_graph_nodes,
    validate_structure,
)


def _node(nid, subagent="research-raven", template="do it", **over):
    return NodeSpec(id=nid, subagent=subagent, prompt_template=template, **over)


def _spec(nodes, params=None):
    return PlaybookSpec(
        name="t",
        description="d",
        mode="dag",
        triggers=Triggers(keywords=["k"]),
        nodes=nodes,
        params=params or {},
    )


def test_unknown_agent_is_an_error():
    """Checked against the agent table the caller passes in.

    ``known_agents=None`` -- no table reachable -- skips the check rather than
    running it against a stand-in list, which is what the four hardcoded names
    this used to default to were: a playbook naming a configured ``claude_code``
    was reported invalid, and one naming a deleted agent was reported fine.
    """
    known = ["research-raven", "code-raven"]
    errors = validate_structure(_spec([_node("a", subagent="gpt-9000")]), known_agents=known)
    assert any("not registered" in e for e in errors)
    assert validate_structure(_spec([_node("a")]), known_agents=known) == []
    assert validate_structure(_spec([_node("a", subagent="gpt-9000")])) == []


def test_reference_must_be_inside_depends_on():
    nodes = [_node("a"), _node("b", template="use {{ a.output }}")]
    errors = validate_graph_nodes(nodes, set())
    assert any("not in its dependsOn" in e for e in errors)
    ok = [_node("a"), _node("b", template="use {{ a.output }}", depends_on=["a"])]
    assert validate_graph_nodes(ok, set()) == []


def test_param_refs_must_be_declared():
    errors = validate_structure(_spec([_node("a", template="research ${params.ghost}")]))
    assert any("ghost" in e for e in errors)
    assert (
        validate_structure(
            _spec([_node("a", template="research ${params.t}")], params={"t": ParamSpec(description="x")})
        )
        == []
    )


def test_cycles_and_unknown_deps_are_errors():
    errors = validate_graph_nodes([_node("a", depends_on=["b"]), _node("b", depends_on=["a"])], set())
    assert any("cycle" in e for e in errors)
    errors = validate_graph_nodes([_node("a", depends_on=["nope"])], set())
    assert any("unknown node" in e for e in errors)


#: Rules 8 and 9 are about how same-instance members relate to each other, so they
#: are exercised against an agent declared stateful -- otherwise rule 7 fires first
#: and the graph is refused before those checks say anything.
_STATEFUL = {"research-raven"}


def test_instance_members_must_form_a_chain():
    # No dependency chain between same-instance members: they would run concurrently.
    concurrent = [_node("a", instance="w"), _node("b", instance="w")]
    errors = validate_graph_nodes(concurrent, set(), stateful_agents=_STATEFUL)
    assert any("no dependency chain" in e for e in errors)
    ok = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", depends_on=["a"])]
    assert validate_graph_nodes(ok, set(), stateful_agents=_STATEFUL) == []


def test_a_continuation_node_may_now_declare_its_own_skills():
    """The old rule 9 is gone with the mechanism that justified it.

    It refused ``skills`` on any member after the first, because a resumed
    raven-loop session keeps the system prompt -- and its skill menu -- from the
    turn that opened it. The engine no longer puts the list there: it folds it
    into the step's own prompt, a user message appended on every node. So a
    continuation node's list now takes effect, and refusing it would reject a
    graph that works.
    """
    late = [
        _node("a", instance="w", skills=["s1"]),
        _node("b", instance="w", skills=["s2"], depends_on=["a"]),
    ]
    assert validate_graph_nodes(late, set(), stateful_agents=_STATEFUL) == []


def test_instance_needs_a_stateful_agent():
    """Rule 7: a handle on an agent that cannot resume promises continuity the run
    cannot deliver -- refused rather than noted, because the author would otherwise
    learn of it from a node that failed to remember the previous one.

    Which agents those are comes from the table. Passing no set at all means no
    table was reachable, and the rule is then skipped rather than applied against
    an empty stand-in -- which is what used to refuse every ``instance`` ever
    written.
    """
    nodes = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", depends_on=["a"])]
    errors = validate_graph_nodes(nodes, set(), stateful_agents=["something-else"])
    assert any("cannot hold a session" in e for e in errors)
    # Fine once the agent is known to be stateful -- and built-in agents are.
    assert validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL) == []
    # And unchecked when there is no table to check against.
    assert validate_graph_nodes(nodes, set()) == []


def test_a_node_cannot_declare_confirm_at_all():
    """The field is gone rather than validated against.

    Its only effect was ever to be rejected -- honouring it needs a runner that can
    pause mid-graph -- so it was a field whose whole purpose was to fail. The gate
    is graph-level (``PlaybookSpec.confirm``), where approving means approving the
    whole graph, which is what the approver is shown.
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _node("publish", confirm=True)


def test_prompt_mode_checks_param_refs_in_prompts():
    spec = PlaybookSpec(
        name="p",
        description="d",
        mode="prompt",
        triggers=Triggers(keywords=["k"]),
        prompts="compose for ${params.ghost}",
    )
    assert any("ghost" in e for e in validate_structure(spec))


def test_unknown_skills_and_mcps_degrade_not_block():
    spec = _spec([_node("a", skills=["known", "ghost-skill"], mcps=["ghost-mcp"])])
    errors, missing = check_assets(spec, known_skills=["known"], known_mcp=[])
    assert errors == []
    assert any("ghost-skill" in m for m in missing) and any("ghost-mcp" in m for m in missing)


def test_a_cycle_among_instance_sharing_nodes_reports_the_cycle():
    """It used to raise StopIteration out of an LLM repair loop.

    `_forms_chain` finds every pair of a mutual cycle "ordered" (each reaches the
    other), so the head generator ran empty and a bare `next()` raised -- escaping
    the compose and generation repair loops as an exception instead of an error
    string they could feed back, and tracebacking `raven playbook validate` on a
    hand-written file.
    """
    nodes = [
        _node("a", depends_on=["b"], instance="h", skills=["s"]),
        _node("b", depends_on=["a"], instance="h"),
    ]

    errors = validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL)

    assert any("cycle" in e for e in errors)


def test_a_shared_session_ordered_through_a_non_member_is_accepted():
    """Same shape as the DAG-side check, kept in step with it.

    Closing over only the group's members calls `draft` and `revise` unordered
    when the edge between them runs through `review`.
    """
    nodes = [
        _node("draft", instance="author", skills=["w"]),
        _node("review", template="critique {{ draft.output }}", depends_on=["draft"]),
        _node("revise", template="revise {{ review.output }}", depends_on=["review"], instance="author"),
    ]

    assert validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL) == []
