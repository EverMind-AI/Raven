"""Contract shape and mode/section pairing for the playbook field definition."""

import pytest
from pydantic import ValidationError

from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.types import slugify


def _dag(**over):
    base = dict(
        name="competitor-scan",
        description="research one competitor on the market and technology fronts in parallel",
        mode="dag",
        triggers=Triggers(keywords=["competitor"]),
        nodes=[NodeSpec(id="scan", subagent="research-raven", prompt_template="research ${params.target}")],
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
