"""The field definition's rule table: graph coherence, references, instances."""

import pytest

from raven.agent.subagent.dag_graph import SubAgentDagSpec, validate_and_order
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.params import fill_param_refs_without_secrets, secret_param_names
from raven.playbook.validate import (
    check_assets,
    unusable_mcp_servers,
    validate_graph_nodes,
    validate_structure,
)


def _node(nid, subagent="research-raven", template="do it", **over):
    return NodeSpec(id=nid, subagent=subagent, node_summary=f"step {nid}", prompt_template=template, **over)


def _spec(nodes, params=None):
    return PlaybookSpec(
        name="t",
        description="d",
        task_summary="run the nodes under test",
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
    assert any("references undeclared dependency 'a'" in e for e in errors)
    ok = [_node("a"), _node("b", template="use {{ a.output }}", depends_on=["a"])]
    assert validate_graph_nodes(ok, set()) == []


def test_path_confinement_waits_for_dispatch_roots(tmp_path) -> None:
    inside = tmp_path / "notes.md"
    reusable_nodes = [
        _node("a", template=f"read {{{{ ref:{inside} }}}}"),
        _node(
            "a",
            template="read {{ inputs.material }}",
            inputs={"material": {"file": str(inside)}},
        ),
    ]

    for node in reusable_nodes:
        assert validate_graph_nodes([node], set()) == []
        spec = SubAgentDagSpec(task_summary="read a reusable path", nodes=[node])
        assert validate_and_order(spec, roots=(str(tmp_path),)) == ["a"]

    outside = tmp_path.parent / "outside.md"
    spec = SubAgentDagSpec(
        task_summary="reject a path outside the session",
        nodes=[_node("a", template=f"read {{{{ ref:{outside} }}}}")],
    )
    with pytest.raises(DagValidationError, match="outside the session workdir"):
        validate_and_order(spec, roots=(str(tmp_path),))


def test_a_blank_node_summary_is_rejected_here_not_only_at_dispatch():
    """``_compose``'s repair loop calls this directly (no ``known_agents``), so a
    composed graph must fail here rather than reach dag dispatch, whose error
    ("call load_playbook again with fills") does not apply to a graph that was
    never loaded from a playbook.
    """
    nodes = [NodeSpec(id="a", subagent="research-raven", node_summary="", prompt_template="do it")]
    errors = validate_graph_nodes(nodes, set())
    assert any("node_summary" in e for e in errors)


def test_param_refs_must_be_declared():
    errors = validate_structure(_spec([_node("a", template="research ${params.ghost}")]))
    assert any("ghost" in e for e in errors)
    assert (
        validate_structure(
            _spec([_node("a", template="research ${params.t}")], params={"t": ParamSpec(description="x")})
        )
        == []
    )


def test_params_must_not_be_duplicated_into_node_inputs() -> None:
    params = {"project_path": ParamSpec(default=".", description="which project directory?")}
    duplicated = _node(
        "detect",
        template="inspect ${params.project_path}",
        inputs={"project_path": "${params.project_path}"},
    )
    errors = validate_structure(_spec([duplicated], params=params))
    assert any("declared but never referenced" in error for error in errors)
    assert any("must not be copied into node inputs" in error for error in errors)

    wrong_repair = _node(
        "detect",
        template="inspect {{ inputs.project_path }}",
        inputs={"project_path": "${params.project_path}"},
    )
    errors = validate_structure(_spec([wrong_repair], params=params))
    assert not any("declared but never referenced" in error for error in errors)
    assert any("must not be copied into node inputs" in error for error in errors)

    correct = _node("detect", template="inspect ${params.project_path}")
    assert validate_structure(_spec([correct], params=params)) == []


def test_cycles_and_unknown_deps_are_errors():
    errors = validate_graph_nodes([_node("a", depends_on=["b"]), _node("b", depends_on=["a"])], set())
    assert any("cycle" in e for e in errors)
    errors = validate_graph_nodes([_node("a", depends_on=["nope"])], set())
    assert any("depends on unknown 'nope'" in e for e in errors)


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


def test_a_continuation_node_cannot_reconfigure_the_shared_skill_menu():
    late = [
        _node("a", instance="w", skills=["s1"]),
        _node("b", instance="w", depends_on=["a"], skills=["later"]),
    ]
    errors = validate_graph_nodes(late, set(), stateful_agents=_STATEFUL)
    assert any("continuing instance 'w'" in error and "opened by node 'a'" in error for error in errors)


def test_a_continuation_node_may_replace_or_clear_its_mcp_grant():
    for later in (["m2"], []):
        nodes = [
            _node("a", instance="w", mcps=["m1"]),
            _node("b", instance="w", depends_on=["a"], mcps=later),
        ]
        assert validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL) == []


@pytest.mark.parametrize(
    "value",
    ["${params.project_path}", "{{ params.project_path }}", {"file": "${params.project_path}"}],
)
def test_every_param_spelling_is_rejected_inside_node_inputs(value: object) -> None:
    params = {"project_path": ParamSpec(default=".", description="which project directory?")}
    node = _node("detect", template="inspect {{ inputs.project_path }}", inputs={"project_path": value})

    errors = validate_structure(_spec([node], params=params))

    assert any("must not be copied into node inputs" in error for error in errors)


def test_only_the_opening_node_may_configure_a_shared_session():
    nodes = [
        _node("a", instance="w", skills=[], mcps=[]),
        _node("b", instance="w", depends_on=["a"]),
    ]
    assert validate_graph_nodes(nodes, set(), stateful_agents=_STATEFUL) == []


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
        task_summary="compose a graph for the named param",
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


# ------------------------------------------------- the playbook's own MCP servers


def _pg(**over):
    from raven.config.schema import MCPServerConfig

    base = dict(command="pg-mcp", env={"PGPASSWORD": "{{ params.PG_PASSWORD }}"})
    base.update(over)
    return MCPServerConfig(**base)


def _with_servers(servers, params=None, nodes=None, **over):
    return PlaybookSpec(
        name="t",
        description="d",
        task_summary="run the nodes under test",
        mode="dag",
        triggers=Triggers(keywords=["k"]),
        nodes=nodes or [_node("a", mcps=["local-pg"])],
        params=params or {"PG_PASSWORD": ParamSpec(type="secret", description="the database password")},
        mcp_servers=servers,
        **over,
    )


def test_a_server_reference_must_name_a_declared_param():
    """Otherwise the reference substitutes to itself and the server is handed
    the literal text as its password.

    A note and not an error: the playbook still runs, without that server. An
    error reaches ``load_playbook`` as a raised ``ValueError`` and the runtime
    answers that by dropping the whole playbook, which is a saved procedure
    disappearing over an optional section.
    """
    spec = _with_servers({"local-pg": _pg(env={"PGPASSWORD": "{{ params.ghost }}"})})
    assert validate_structure(spec) == [], "an unusable server must not invalidate the playbook"
    unusable = unusable_mcp_servers(spec)
    assert "local-pg" in unusable
    assert "env.PGPASSWORD" in unusable["local-pg"] and "ghost" in unusable["local-pg"]
    assert unusable_mcp_servers(_with_servers({"local-pg": _pg()})) == {}


def test_a_header_reference_is_checked_like_an_env_one():
    spec = _with_servers(
        {"api": _pg(command="", url="https://x.test/mcp", env={}, headers={"X-Key": "${params.ghost}"})}
    )
    assert "headers.X-Key" in unusable_mcp_servers(spec)["api"]
    assert validate_structure(spec) == []


def test_a_definition_with_no_transport_is_dropped_at_the_file_not_at_dispatch():
    """Named at the file, where the cause is, rather than surfacing as
    ``invalid_transport`` a node or two into the run -- but dropped, so the rest
    of the playbook still runs."""
    spec = _with_servers({"local-pg": _pg(command="")})
    assert "needs a command" in unusable_mcp_servers(spec)["local-pg"]
    assert validate_structure(spec) == []


def test_a_secret_referenced_from_a_node_prompt_is_withheld_not_refused():
    """A prompt is dispatched to a sub-agent and persisted with the run, so a
    secret reaching one has left the host. It is withheld at fill time.

    Not refused at the file: an error here reaches ``load_playbook`` as a raised
    ``ValueError`` and the runtime answers that by dropping the whole playbook
    out of the library. ``mcpServers`` is an optional section on top of a
    playbook that otherwise runs, and a reference written in the wrong place has
    to cost that reference, not the procedure.
    """
    spec = _with_servers({"local-pg": _pg()}, nodes=[_node("a", template="connect with {{ params.PG_PASSWORD }}")])
    assert validate_structure(spec) == [], "the playbook still loads and runs"

    filled, withheld = fill_param_refs_without_secrets(
        spec.nodes[0].prompt_template, {"PG_PASSWORD": "s3cr3t"}, secret_param_names(spec)
    )
    assert "s3cr3t" not in filled, "the value must never reach a work order"
    assert withheld == ["PG_PASSWORD"]


def test_a_secret_referenced_from_prompt_mode_guidance_is_withheld_too():
    """The filled guidance is what a compose call sends to a provider, and in a
    conversation it is returned to the caller as the reply. Same treatment, same
    reason: withheld where it would leave, and the playbook keeps working."""
    spec = PlaybookSpec(
        name="t",
        description="d",
        task_summary="compose a graph",
        mode="prompt",
        triggers=Triggers(keywords=["k"]),
        prompts="one node that connects with ${params.PG_PASSWORD}",
        params={"PG_PASSWORD": ParamSpec(type="secret", description="the database password")},
    )
    assert validate_structure(spec) == []

    filled, withheld = fill_param_refs_without_secrets(
        spec.prompts, {"PG_PASSWORD": "s3cr3t"}, secret_param_names(spec)
    )
    assert "s3cr3t" not in filled
    assert withheld == ["PG_PASSWORD"]


def test_the_same_secret_still_reaches_an_mcp_server_env():
    """The one place its value may land. Withholding it from prompts must not
    have withheld it from the section the whole type exists for."""
    from raven.playbook.mcp import playbook_mcp_servers

    spec = _with_servers({"local-pg": _pg(env={"PGPASSWORD": "{{ params.PG_PASSWORD }}"})})
    servers = playbook_mcp_servers(spec, {"PG_PASSWORD": "s3cr3t"})
    assert servers["local-pg"].env == {"PGPASSWORD": "s3cr3t"}


def test_a_non_secret_param_is_still_free_to_appear_in_a_prompt():
    spec = _with_servers(
        {"local-pg": _pg(env={})},
        params={"region": ParamSpec(description="which region?")},
        nodes=[_node("a", template="audit ${params.region}")],
    )
    assert validate_structure(spec) == []


def test_the_braces_spelling_is_checked_like_the_dollar_one():
    spec = _with_servers({"local-pg": _pg(env={})}, params={}, nodes=[_node("a", template="audit {{ params.ghost }}")])
    assert any("ghost" in e for e in validate_structure(spec))


def test_a_playbook_defined_server_is_not_an_unknown_capability():
    """``mcps: [local-pg]`` naming a server the file itself defines is not the
    receiving machine's inventory to judge."""
    spec = _with_servers({"local-pg": _pg()})
    errors, missing = check_assets(spec, known_skills=[], known_mcp=[])
    assert errors == []
    assert missing == []


def _prompt_mode(servers=None, params=None):
    return PlaybookSpec(
        name="t",
        description="d",
        task_summary="compose a graph",
        mode="prompt",
        triggers=Triggers(keywords=["k"]),
        prompts="one node per table, each with mcps: [local-pg]",
        params=params or {},
        mcp_servers=servers or {},
    )


def test_prompt_mode_cannot_carry_server_definitions_and_they_are_dropped():
    """The hand-off that delivers them is only reachable when the executor
    dispatches the graph itself. In a conversation prompt mode returns guidance
    and the model dispatches in a later turn, through a public tool with no
    ``mcpServers`` parameter and no run scope left to read.

    So the section cannot be honoured -- and is dropped rather than refused. The
    playbook's own work does not depend on it, and refusing takes the whole file
    out of the library over a section it will run fine without.
    """
    spec = _prompt_mode({"local-pg": _pg(env={})})

    assert validate_structure(spec) == [], "the playbook is still valid without the section"
    why = unusable_mcp_servers(spec)["local-pg"]
    assert "prompt-mode playbook cannot carry server definitions" in why
    assert "mode 'dag'" in why


def test_prompt_mode_without_the_section_is_still_valid():
    assert validate_structure(_prompt_mode()) == []


def test_dag_mode_still_carries_server_definitions():
    assert validate_structure(_with_servers({"local-pg": _pg()})) == []


def test_the_library_keeps_a_playbook_that_references_a_secret_from_a_prompt(tmp_path):
    """Where refusing it used to bite. The store gates both the write and every
    read, so an error was the file disappearing -- over an optional section."""
    from raven.playbook.store import PlaybookStore

    store = PlaybookStore(tmp_path)
    spec = _with_servers({"local-pg": _pg()}, nodes=[_node("a", template="connect with {{ params.PG_PASSWORD }}")])
    store.save(spec)

    loaded = store.load("t")
    assert loaded.nodes[0].prompt_template == "connect with {{ params.PG_PASSWORD }}", (
        "the file keeps what the author wrote; the withholding happens when it is filled"
    )
    assert "PG_PASSWORD" in loaded.params


def test_the_library_keeps_a_prompt_mode_playbook_and_drops_only_its_servers(tmp_path):
    """Where it has to bite: the store gates both the write and every read, so a
    refusal here is the file disappearing. It is saved and it loads; the section
    that could not be honoured is what is gone."""
    from raven.playbook.store import PlaybookStore

    store = PlaybookStore(tmp_path)
    store.save(_prompt_mode({"local-pg": _pg(env={})}))
    assert (tmp_path / "t").exists(), "the playbook must survive an unusable section"

    loaded = store.load("t")
    assert loaded.mode == "prompt"
    assert loaded.mcp_servers == {}, "the section it cannot honour is dropped on load"


def _role(label, name="code-raven", template="do the work", **over):
    return {"as": label, "name": name, "promptTemplate": template, **over}


def _rounds_spec(roles, *, params=None, verify=None, memory=None):
    return PlaybookSpec(
        name="t",
        description="d",
        task_summary="run the rounds under test",
        mode="stint",
        triggers=Triggers(keywords=["k"]),
        roles=roles,
        params=params or {},
        verify=verify,
        memory=memory,
    )


def test_a_role_is_cast_from_the_agent_table():
    known = ["research-raven", "code-raven"]
    errors = validate_structure(_rounds_spec([_role("dev", name="gpt-9000")]), known_agents=known)
    assert any("no agent named 'gpt-9000'" in e for e in errors)
    assert validate_structure(_rounds_spec([_role("dev")]), known_agents=known) == []
    # No table reachable means the names are not checked at all, as for nodes.
    assert validate_structure(_rounds_spec([_role("dev", name="gpt-9000")])) == []


def test_a_role_prompt_may_only_name_declared_params():
    spec = _rounds_spec([_role("dev", template="build ${params.target}")])
    assert any("params.target names no declared param" in e for e in validate_structure(spec))
    ok = _rounds_spec(
        [_role("dev", template="build ${params.target}")],
        params={"target": ParamSpec(required=True, description="which target?")},
    )
    assert validate_structure(ok) == []


def test_a_role_with_nothing_to_say_is_refused():
    assert any("promptTemplate" in e for e in validate_structure(_rounds_spec([_role("dev", template="  ")])))


def test_one_path_has_one_owner():
    """An owner beside an appender is the shape the grid is built on; two
    owners is the one overlap with no reading."""
    clash = _rounds_spec([_role("a", owns=["src/**"]), _role("b", owns=["src/**"])])
    assert any("is owned by a, b" in e for e in validate_structure(clash))
    # Ordered, so the grid is the only thing under test: two roles that can run
    # at once are refused for a different reason entirely, and an unordered
    # pair here would pass or fail on that instead.
    fine = _rounds_spec([_role("a", owns=["src/**"]), _role("b", appends=["src/**"], dependsOn=["a"])])
    assert validate_structure(fine) == []


def test_checks_nobody_runs_are_reported_rather_than_carried():
    spec = _rounds_spec([_role("dev")], verify=[{"name": "build", "run": "make"}])
    assert any("no role runs any of them" in e for e in validate_structure(spec))


def test_two_checks_of_one_name_are_refused():
    spec = _rounds_spec(
        [_role("dev", verifyAfter=["build"])],
        verify=[{"name": "build", "run": "make"}, {"name": "build", "run": "ninja"}],
    )
    assert any("share the name build" in e for e in validate_structure(spec))


def test_one_memory_file_carries_one_set_of_limits():
    spec = _rounds_spec(
        [_role("dev")],
        memory=[{"path": "JOURNAL.md", "recentRounds": 2}, {"path": "JOURNAL.md", "recentRounds": 5}],
    )
    assert any("JOURNAL.md twice" in e for e in validate_structure(spec))


class TestTwoRolesAtOnceInOneCheckout:
    """A stint has one checkout and the roles share it, so what a role wrote is
    measured as the difference the tree shows. Two at once is two sets of
    changes in one tree."""

    @staticmethod
    def _spec(roles):
        from raven.playbook.types import PlaybookSpec

        return PlaybookSpec.model_validate(
            {
                "name": "p",
                "description": "d",
                "taskSummary": "t",
                "mode": "stint",
                "triggers": {"keywords": ["k"]},
                "roles": roles,
            }
        )

    def test_a_chain_is_fine(self):
        """The ordinary shape, and the one the shipped playbook uses."""
        chain = [
            {"as": "planner", "name": "echo", "promptTemplate": "p", "owns": ["reports/**"]},
            {"as": "dev", "name": "echo", "dependsOn": ["planner"], "promptTemplate": "p", "owns": ["src/**"]},
            {"as": "qa", "name": "echo", "dependsOn": ["dev"], "promptTemplate": "p", "owns": ["qa/**"]},
        ]

        assert validate_structure(self._spec(chain), known_agents=["echo"]) == []

    def test_two_enforced_roles_that_can_run_at_once_are_refused(self):
        """Observed before it was refused: two roles with disjoint `owns`, each
        writing only what it owned, and the first judged came back as
        `dev-a wrote 1 path(s) it may not write: src/b/work.py` -- with
        `src/b/work.py` deleted. Nothing errored; it read as a role that would
        not stay in its lane."""
        fan = [
            {"as": "planner", "name": "echo", "promptTemplate": "p", "owns": ["reports/**"]},
            {"as": "dev-a", "name": "echo", "dependsOn": ["planner"], "promptTemplate": "p", "owns": ["src/a/**"]},
            {"as": "dev-b", "name": "echo", "dependsOn": ["planner"], "promptTemplate": "p", "owns": ["src/b/**"]},
        ]

        [error] = validate_structure(self._spec(fan), known_agents=["echo"])

        assert "dev-a and dev-b" in error
        # Both ways out, because which one is right is the author's call.
        assert "dependsOn" in error and "enforce.write: soft" in error

    def test_a_read_fence_nobody_holds_is_refused(self):
        """`enforce.read: hard` swaps the guard's wording for "refused at the
        tool gate" and nothing else happens: no gate refuses a read. A role that
        believed it would stop opening the file it needed, so the grade is
        refused where it is written rather than read out as a promise."""
        spec = self._spec(
            [
                {
                    "as": "qa",
                    "name": "echo",
                    "promptTemplate": "p",
                    "reads": ["src/**"],
                    "enforce": {"read": "hard"},
                }
            ]
        )

        [error] = validate_structure(spec, known_agents=["echo"])

        assert "qa asks for enforce.read: hard" in error
        assert "nothing enforces yet" in error and "Use soft" in error

    def test_the_soft_default_is_what_the_shipped_shape_uses(self):
        assert (
            validate_structure(
                self._spec([{"as": "qa", "name": "echo", "promptTemplate": "p", "reads": ["src/**"]}]),
                known_agents=["echo"],
            )
            == []
        )

    def test_running_at_once_is_fine_where_nothing_is_being_enforced(self):
        """The refusal is about the measurement, not about the concurrency."""
        fan = [
            {"as": "a", "name": "echo", "promptTemplate": "p", "owns": ["a/**"], "enforce": {"write": "soft"}},
            {"as": "b", "name": "echo", "promptTemplate": "p", "owns": ["b/**"], "enforce": {"write": "soft"}},
        ]

        assert validate_structure(self._spec(fan), known_agents=["echo"]) == []

    def test_one_enforced_role_beside_a_free_one_is_still_refused(self):
        """The enforced one is graded against the whole tree, so it reverts the
        other's work whether or not the other is held to anything."""
        fan = [
            {"as": "held", "name": "echo", "promptTemplate": "p", "owns": ["a/**"]},
            {"as": "free", "name": "echo", "promptTemplate": "p"},
        ]

        assert validate_structure(self._spec(fan), known_agents=["echo"]) != []

    def test_an_indirect_order_counts_as_an_order(self):
        """`qa` waits on `dev` which waits on `planner`, so qa and planner never
        overlap even though qa does not name planner."""
        chain = [
            {"as": "planner", "name": "echo", "promptTemplate": "p", "owns": ["reports/**"]},
            {"as": "dev", "name": "echo", "dependsOn": ["planner"], "promptTemplate": "p", "owns": ["src/**"]},
            {"as": "qa", "name": "echo", "dependsOn": ["dev"], "promptTemplate": "p", "owns": ["qa/**"]},
        ]

        assert validate_structure(self._spec(chain), known_agents=["echo"]) == []


class TestIsolationAndBoundaries:
    """`isolation: none` runs in the person's own checkout, on their branch."""

    @staticmethod
    def _spec(roles, isolation=None):
        from raven.playbook.types import PlaybookSpec

        body = {
            "name": "p",
            "description": "d",
            "taskSummary": "t",
            "mode": "stint",
            "triggers": {"keywords": ["k"]},
            "roles": roles,
        }
        if isolation is not None:
            body["isolation"] = isolation
        return PlaybookSpec.model_validate(body)

    def test_a_hard_boundary_over_the_person_s_own_branch_is_refused(self):
        """Undoing a stray write is putting the tree back, and that tree is
        theirs -- so their uncommitted work goes back with it."""
        roles = [{"as": "dev", "name": "echo", "promptTemplate": "p", "owns": ["src/**"]}]

        errors = validate_structure(self._spec(roles, isolation="none"), known_agents=["echo"])

        assert len(errors) == 1
        assert "isolation: none" in errors[0]
        assert "isolation: branch" in errors[0] and "enforce.write: soft" in errors[0]

    def test_the_same_roles_are_fine_in_a_branch_of_the_run_s_own(self):
        roles = [{"as": "dev", "name": "echo", "promptTemplate": "p", "owns": ["src/**"]}]

        assert validate_structure(self._spec(roles, isolation="branch"), known_agents=["echo"]) == []

    def test_a_soft_boundary_may_run_in_the_person_s_own_branch(self):
        """Nothing is undone, so nothing of theirs can be undone with it."""
        roles = [
            {
                "as": "dev",
                "name": "echo",
                "promptTemplate": "p",
                "owns": ["src/**"],
                "enforce": {"write": "soft"},
            }
        ]

        assert validate_structure(self._spec(roles, isolation="none"), known_agents=["echo"]) == []

    def test_a_playbook_that_declares_nothing_may_run_in_the_person_s_own_branch(self):
        roles = [{"as": "dev", "name": "echo", "promptTemplate": "p"}]

        assert validate_structure(self._spec(roles, isolation="none"), known_agents=["echo"]) == []
