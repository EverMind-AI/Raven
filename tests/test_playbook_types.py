"""Contract shape and mode/section pairing for the playbook field definition."""

import pytest
from pydantic import ValidationError

from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.types import slugify


def _dag(**over):
    base = dict(
        name="competitor-scan",
        description="research one competitor on the market and technology fronts in parallel",
        task_summary="research the named competitor and report what was found",
        mode="dag",
        triggers=Triggers(keywords=["competitor"]),
        nodes=[
            NodeSpec(
                id="scan",
                subagent="research-raven",
                node_summary="research the target",
                prompt_template="research ${params.target}",
            )
        ],
        params={"target": ParamSpec(required=True, description="which competitor should be scanned?")},
    )
    base.update(over)
    return PlaybookSpec(**base)


def test_dag_requires_nodes_and_forbids_prompts():
    with pytest.raises(ValidationError, match="requires non-empty nodes"):
        _dag(nodes=None)
    with pytest.raises(ValidationError, match="must not carry prompts"):
        _dag(prompts="assembly guidance")


def test_prompt_requires_prompts_and_forbids_nodes():
    spec = _dag(mode="prompt", nodes=None, prompts="layer one: a single breadth-scan node")
    assert spec.prompts
    with pytest.raises(ValidationError, match="requires non-empty prompts"):
        _dag(mode="prompt", nodes=None, prompts="  ")
    with pytest.raises(ValidationError, match="must not carry nodes"):
        _dag(mode="prompt", prompts="x" * 10)


def test_name_must_be_a_slug():
    with pytest.raises(ValidationError):
        _dag(name="Competitor Scan")
    # Non-ASCII segments (e.g. CJK) fall out of the slug alphabet entirely.
    assert slugify("\u6bcf\u5468 Feedback \u5206\u6790") == "feedback"


def test_enum_param_needs_values():
    with pytest.raises(ValidationError, match="non-empty 'enum'"):
        ParamSpec(type="enum", description="which focus?")
    with pytest.raises(ValidationError, match="'enum' given"):
        ParamSpec(type="string", enum=["a"], description="x")


def test_triggers_need_at_least_one_keyword():
    with pytest.raises(ValidationError):
        Triggers(keywords=[])


def test_camel_aliases_round_trip():
    node = NodeSpec.model_validate(
        {"id": "a", "subagent": "code-raven", "promptTemplate": "do the work", "dependsOn": [], "mcps": ["github"]}
    )
    assert node.prompt_template == "do the work"
    dumped = node.model_dump(by_alias=True)
    assert "promptTemplate" in dumped and "dependsOn" in dumped


def test_block_dump_excludes_frontmatter_fields():
    data = _dag().block_dump()
    for absent in ("name", "description"):
        assert absent not in data
    assert data["mode"] == "dag"
    assert data["nodes"][0]["promptTemplate"].startswith("research")


def test_lifecycle_fields_are_rejected():
    """No status, no provenance: local state lives in config, not the file."""
    with pytest.raises(ValidationError):
        _dag(status="ready")
    with pytest.raises(ValidationError):
        _dag(provenance={"blockingQuestions": ["what for?"]})


def test_a_playbook_needs_a_task_summary_in_either_mode() -> None:
    with pytest.raises(ValidationError):
        _dag(task_summary="")
    with pytest.raises(ValidationError):
        _dag(mode="prompt", nodes=None, prompts="layer one: a single breadth-scan node", task_summary="")


def test_task_summary_is_a_machine_field_not_frontmatter() -> None:
    spec = _dag()
    assert spec.block_dump()["taskSummary"] == spec.task_summary
    assert PlaybookSpec.FRONTMATTER_FIELDS == ("name", "description")


def test_a_secret_param_may_not_carry_a_default():
    """A default is a value, and a value in the file travels with the file."""
    with pytest.raises(ValidationError, match="must not carry a default"):
        ParamSpec(type="secret", default="hunter2", description="the database password")
    assert ParamSpec(type="secret", description="the database password").default is None


def test_a_playbook_may_ship_its_own_mcp_servers():
    spec = PlaybookSpec.model_validate(
        {
            "name": "audit",
            "description": "audit the analytics database",
            "taskSummary": "run the analytics audit",
            "mode": "dag",
            "triggers": {"keywords": ["audit"]},
            "params": {"PG_PASSWORD": {"type": "secret", "description": "the database password"}},
            "mcpServers": {
                "local-pg": {
                    "command": "pg-mcp",
                    "args": ["--db", "analytics"],
                    "env": {"PGPASSWORD": "{{ params.PG_PASSWORD }}"},
                    "toolTimeout": 45,
                }
            },
            "nodes": [
                {
                    "id": "a",
                    "subagent": "research-raven",
                    "nodeSummary": "audit",
                    "promptTemplate": "audit it",
                    "mcps": ["local-pg"],
                }
            ],
        }
    )
    # The host's own server model, not a second definition of the same thing.
    assert spec.mcp_servers["local-pg"].command == "pg-mcp"
    assert spec.mcp_servers["local-pg"].tool_timeout == 45

    # The block keeps the reference and only what the author wrote: a server
    # config carries a full default set, and dumping it whole turned a
    # three-line definition into twenty.
    dumped = spec.block_dump()["mcpServers"]["local-pg"]
    assert dumped == {
        "command": "pg-mcp",
        "args": ["--db", "analytics"],
        "env": {"PGPASSWORD": "{{ params.PG_PASSWORD }}"},
        "toolTimeout": 45,
    }


def test_a_playbook_without_mcp_servers_writes_no_such_section():
    assert "mcpServers" not in _dag().block_dump()


def _rounds(**over):
    base = dict(
        name="game-dev",
        description="drive a game project forward one round at a time",
        task_summary="run the next rounds of the game project",
        mode="stint",
        triggers=Triggers(keywords=["game"]),
        roles=[
            {"as": "planner", "name": "research-raven", "promptTemplate": "pick the work", "owns": ["reports/**"]},
            {
                "as": "builder",
                "name": "code-raven",
                "dependsOn": ["planner"],
                "promptTemplate": "do the work",
                "owns": ["src/**"],
                "verifyAfter": ["build"],
            },
        ],
        verify=[{"name": "build", "run": "make build"}],
        stop={"maxRounds": 30},
    )
    base.update(over)
    return PlaybookSpec(**base)


def test_rounds_requires_roles_and_forbids_a_graph():
    spec = _rounds()
    assert [role.label for role in spec.roles] == ["planner", "builder"]
    with pytest.raises(ValidationError, match="requires non-empty roles"):
        _rounds(roles=[])
    with pytest.raises(ValidationError, match="carries roles, not nodes or prompts"):
        _rounds(prompts="assembly guidance")


def test_the_other_modes_may_not_carry_a_rounds_section():
    """Refused rather than ignored: a dag playbook carrying `verify` would read
    as one that runs commands, and would not."""
    with pytest.raises(ValidationError, match="must not carry verify"):
        _dag(verify=[{"name": "build", "run": "make build"}])


def test_a_role_table_that_could_not_compile_is_refused_at_load():
    with pytest.raises(ValidationError, match="share the label"):
        _rounds(
            roles=[
                {"as": "dev", "name": "code-raven", "promptTemplate": "a"},
                {"as": "dev", "name": "research-raven", "promptTemplate": "b"},
            ]
        )
    with pytest.raises(ValidationError, match="which no role answers to"):
        _rounds(roles=[{"as": "dev", "name": "code-raven", "promptTemplate": "a", "dependsOn": ["ghost"]}])
    with pytest.raises(ValidationError, match="depends on itself"):
        _rounds(roles=[{"as": "dev", "name": "code-raven", "promptTemplate": "a", "dependsOn": ["dev"]}])
    with pytest.raises(ValidationError, match="in a circle"):
        _rounds(
            roles=[
                {"as": "a", "name": "code-raven", "promptTemplate": "x", "dependsOn": ["b"]},
                {"as": "b", "name": "code-raven", "promptTemplate": "y", "dependsOn": ["a"]},
            ]
        )


def test_a_role_cannot_be_measured_by_a_check_nobody_declared():
    with pytest.raises(ValidationError, match="which no verify entry names"):
        _rounds(verify=[{"name": "tests", "run": "pytest"}])


def test_a_playbook_that_runs_commands_keeps_its_one_approval():
    """`verify` is shell, and the plan's single confirm is what covers it."""
    with pytest.raises(ValidationError, match="must keep confirm: true"):
        _rounds(confirm=False)
    assert _rounds(verify=None, roles=[{"as": "dev", "name": "code-raven", "promptTemplate": "a"}], confirm=False)


def test_stop_until_is_a_marker_a_role_writes_not_an_expression():
    assert _rounds(stop={"until": "verdict"}).stop.until == "verdict"
    with pytest.raises(ValidationError, match="reads as one"):
        _rounds(stop={"until": "tasks_open == 0"})


def test_the_round_budget_has_a_ceiling_a_playbook_cannot_raise():
    assert _rounds(stop=None).stop is None
    with pytest.raises(ValidationError):
        _rounds(stop={"maxRounds": 100})


def test_a_role_row_is_a_delegate_row_with_the_round_on_it():
    """Decision 2: one description of a worker, so the two cannot drift."""
    from raven.playbook.agent_spec import DelegateEntry
    from raven.playbook.stint_spec import RoleEntry

    assert issubclass(RoleEntry, DelegateEntry)
    assert set(DelegateEntry.model_fields) <= set(RoleEntry.model_fields)
    # And the parent stays as narrow as it was: a field here is a field the
    # per-turn worker-table generator could emit.
    assert set(DelegateEntry.model_fields) == {"as_", "name", "playbook"}


def test_the_shipped_developer_is_judged_by_every_directory_its_guard_may_grant() -> None:
    """The guard file grants the Builder whichever of `SOURCE_DIRS` the project
    has (or the greenfield three); the playbook row is what it is judged by. A
    row narrower than the grant is the two-rosters trap with a directory in
    place of a ledger: a Builder that put its checks under `tools/`, where its
    standing orders said it could, had them undone as a stray write."""
    from raven.playbook.store import BUILTIN_ROOT, PlaybookStore
    from raven.stint.bootstrap import GREENFIELD_DIRS, SOURCE_DIRS

    spec = PlaybookStore(BUILTIN_ROOT.parent / "nowhere", builtin_root=BUILTIN_ROOT).load("long-horizon-dev-stint")
    builder = next(role for role in spec.roles or [] if role.label == "builder")
    granted = {f"{name}/**" for name in SOURCE_DIRS} | set(GREENFIELD_DIRS)
    missing = sorted(granted - set(builder.owns))
    assert missing == [], f"the builder may be told it owns {missing} and would be judged without them"


def test_the_shipped_playbook_grades_by_the_same_artifacts_its_guards_name() -> None:
    """The one list written twice: `OUTPUT_DIRS` generates the guard files a role
    reads, and `rounds` repeats it by hand for the roster it is judged by.

    They drifted. `OUTPUT_DIRS` grew `builds` and `replays`; the playbook did
    not, so a Builder that put its replay evidence where its own standing
    orders call an artifact directory had it reverted as a stray write -- by a
    list the role is never shown. The playbook's own comment warns about exactly
    this ("two rosters -- the one it is told and the one it is judged by"), which
    is why this is a test rather than a note.
    """
    from raven.playbook.store import BUILTIN_ROOT, PlaybookStore
    from raven.stint.bootstrap import OUTPUT_DIRS

    spec = PlaybookStore(BUILTIN_ROOT.parent / "nowhere", builtin_root=BUILTIN_ROOT).load("long-horizon-dev-stint")

    for role in spec.roles or []:
        missing = [f"{name}/**" for name in OUTPUT_DIRS if f"{name}/**" not in role.artifacts]
        assert missing == [], f"{role.label} is judged without {missing}, and its guard file names them"
