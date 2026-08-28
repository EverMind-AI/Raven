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
