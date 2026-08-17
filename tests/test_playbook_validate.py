"""The field definition's rule table: graph coherence, references, instances."""

from raven.memory_engine.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.memory_engine.playbook.validate import (
    check_assets,
    check_immutable_region,
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
    nodes = [_node("a"), _node("b", template="用 {{ a.output }}")]
    errors = validate_graph_nodes(nodes, set())
    assert any("not in its dependsOn" in e for e in errors)
    ok = [_node("a"), _node("b", template="用 {{ a.output }}", depends_on=["a"])]
    assert validate_graph_nodes(ok, set()) == []


def test_param_refs_must_be_declared():
    errors = validate_structure(_spec([_node("a", template="调研 ${params.ghost}")]))
    assert any("ghost" in e for e in errors)
    assert (
        validate_structure(_spec([_node("a", template="调研 ${params.t}")], params={"t": ParamSpec(description="x")}))
        == []
    )


def test_cycles_and_unknown_deps_are_errors():
    errors = validate_graph_nodes([_node("a", depends_on=["b"]), _node("b", depends_on=["a"])], set())
    assert any("cycle" in e for e in errors)
    errors = validate_graph_nodes([_node("a", depends_on=["nope"])], set())
    assert any("unknown node" in e for e in errors)


def test_instance_rules_chain_and_toolset():
    # No dependency chain between same-instance members: they would run concurrently.
    concurrent = [_node("a", instance="w"), _node("b", instance="w")]
    assert any("no dependency chain" in e for e in validate_graph_nodes(concurrent, set()))
    # Chained but with different skills: a session's toolset is fixed at start.
    mixed = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", depends_on=["a"])]
    assert any("different skills" in e for e in validate_graph_nodes(mixed, set()))
    ok = [_node("a", instance="w", skills=["s1"]), _node("b", instance="w", skills=["s1"], depends_on=["a"])]
    assert validate_graph_nodes(ok, set()) == []


def test_prompt_mode_checks_param_refs_in_prompts():
    spec = PlaybookSpec(
        name="p",
        description="d",
        mode="prompt",
        triggers=Triggers(keywords=["k"]),
        prompts="为 ${params.ghost} 组图",
    )
    assert any("ghost" in e for e in validate_structure(spec))


def test_unknown_skills_and_mcps_degrade_not_block():
    spec = _spec([_node("a", skills=["known", "ghost-skill"], mcps=["ghost-mcp"])])
    errors, missing = check_assets(spec, known_skills=["known"], known_mcp=[])
    assert errors == []
    assert any("ghost-skill" in m for m in missing) and any("ghost-mcp" in m for m in missing)


def test_immutable_region_survives_revision():
    old = _spec([_node("a")])
    old = old.model_copy(
        update={"provenance": old.provenance.model_copy(update={"specified_by_user": ["每周一跑", "只查P0"]})}
    )
    new = old.model_copy(update={"provenance": old.provenance.model_copy(update={"specified_by_user": ["每周一跑"]})})
    lost = check_immutable_region(old, new)
    assert len(lost) == 1 and "只查P0" in lost[0]
