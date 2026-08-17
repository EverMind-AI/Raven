"""Contract shape and mode/section pairing for the playbook field definition."""

import pytest
from pydantic import ValidationError

from raven.memory_engine.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.memory_engine.playbook.types import slugify


def _dag(**over):
    base = dict(
        name="competitor-scan",
        description="对一家竞品分头做市场面与技术面调研",
        mode="dag",
        triggers=Triggers(keywords=["竞品"]),
        nodes=[NodeSpec(id="scan", agent="research-raven", prompt_template="调研 ${params.target}")],
        params={"target": ParamSpec(required=True, description="要扫描的竞品名称")},
    )
    base.update(over)
    return PlaybookSpec(**base)


def test_dag_requires_nodes_and_forbids_prompts():
    with pytest.raises(ValidationError, match="requires non-empty nodes"):
        _dag(nodes=None)
    with pytest.raises(ValidationError, match="must not carry prompts"):
        _dag(prompts="组图指导")


def test_prompt_requires_prompts_and_forbids_nodes():
    spec = _dag(mode="prompt", nodes=None, prompts="第一层一个节点广度扫描")
    assert spec.prompts
    with pytest.raises(ValidationError, match="requires non-empty prompts"):
        _dag(mode="prompt", nodes=None, prompts="  ")
    with pytest.raises(ValidationError, match="must not carry nodes"):
        _dag(mode="prompt", prompts="x" * 10)


def test_name_must_be_a_slug():
    with pytest.raises(ValidationError):
        _dag(name="Competitor Scan")
    assert slugify("每周 Feedback 分析") == "feedback"


def test_enum_param_needs_values():
    with pytest.raises(ValidationError, match="non-empty 'enum'"):
        ParamSpec(type="enum", description="侧重方向")
    with pytest.raises(ValidationError, match="'enum' given"):
        ParamSpec(type="string", enum=["a"], description="x")


def test_triggers_need_at_least_one_keyword():
    with pytest.raises(ValidationError):
        Triggers(keywords=[])


def test_camel_aliases_round_trip():
    node = NodeSpec.model_validate(
        {"id": "a", "agent": "code-raven", "promptTemplate": "干活", "dependsOn": [], "mcps": ["github"]}
    )
    assert node.prompt_template == "干活"
    dumped = node.model_dump(by_alias=True)
    assert "promptTemplate" in dumped and "dependsOn" in dumped


def test_block_dump_excludes_frontmatter_and_sidecar_fields():
    data = _dag().block_dump()
    for absent in ("name", "description", "status", "provenance"):
        assert absent not in data
    assert data["mode"] == "dag"
    assert data["nodes"][0]["promptTemplate"].startswith("调研")


def test_blocking_questions_pin_draft():
    with pytest.raises(ValidationError, match="draft"):
        _dag(status="ready", provenance={"blockingQuestions": ["用途?"]})
