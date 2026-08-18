"""The field definition's rule table: graph coherence, references, instances."""

from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.validate import (
    check_assets,
    validate_graph_nodes,
    validate_structure,
)


def _node(nid, agent="research-raven", template="do it", **over):
    return NodeSpec(id=nid, agent=agent, prompt_template=template, **over)


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
    errors = validate_structure(_spec([_node("a", agent="gpt-9000")]))
    assert any("not registered" in e for e in errors)


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


def test_instance_rules_chain_and_toolset():
    # No dependency chain between same-instance members: they would run concurrently.
    concurrent = [_node("a", instance="w"), _node("b", instance="w")]
    errors = validate_graph_nodes(concurrent, set(), stateful_agents=_STATEFUL)
    assert any("no dependency chain" in e for e in errors)
    # Chained but with different skills: a session's toolset is fixed at start.
    mixed = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", depends_on=["a"])]
    assert any("different skills" in e for e in validate_graph_nodes(mixed, set(), stateful_agents=_STATEFUL))
    ok = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", skills=["s1"], depends_on=["a"])]
    assert validate_graph_nodes(ok, set(), stateful_agents=_STATEFUL) == []


def test_instance_needs_a_stateful_agent():
    """Rule 7. Every builtin runs in-process with no resume, so a handle on one
    promises continuity the run cannot deliver -- refused rather than noted,
    because the author would otherwise learn of it from a node that failed to
    remember the previous one."""
    nodes = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", skills=["s1"], depends_on=["a"])]
    errors = validate_graph_nodes(nodes, set())
    assert any("cannot hold a session" in e for e in errors)
    # The same graph is fine once the agent is known to be stateful.
    assert validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL) == []


def test_node_confirm_is_refused_while_unenforceable():
    """A gate exists to stop an irreversible step. Downgrading it to a note means
    the note arrives after the step ran, so an unenforceable gate fails the graph."""
    errors = validate_graph_nodes([_node("publish", confirm=True)], set())
    assert any("cannot enforce a node-level gate" in e for e in errors)


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
