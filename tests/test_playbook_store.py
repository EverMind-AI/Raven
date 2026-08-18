"""Round-trip fidelity for the ``playbook.md`` three-region layout.

The load side parses the frontmatter region with ``yaml.safe_load``, so the save
side has to emit YAML rather than interpolate strings into it. ``description``
is model-written prose, which makes the characters YAML reserves -- a colon, a
leading ``#``, quotes, a newline -- ordinary content rather than edge cases.
Each of those produced a file that could not be read back, so each gets a case
here.
"""

import pytest

from raven.memory_engine.playbook import NodeSpec, ParamSpec, PlaybookSpec, PlaybookStore, Triggers


def _spec(description: str) -> PlaybookSpec:
    return PlaybookSpec(
        name="competitor-scan",
        description=description,
        mode="dag",
        triggers=Triggers(keywords=["竞品"]),
        nodes=[NodeSpec(id="scan", agent="research-raven", prompt_template="调研 ${params.target}")],
        params={"target": ParamSpec(required=True, description="要扫描的竞品名称")},
    )


#: label -> description. Every entry is a character YAML reserves but prose
#: uses freely; ``at_cap`` guards the other end -- the contract caps the field at
#: 200 characters, and a value sitting on the cap must not be folded or trimmed.
_DESCRIPTIONS = {
    "colon": "当用户要做尽调时: 先广度扫描, 再深挖存疑项",
    "full_width_colon": "触发场景：想快速了解某家公司",
    "leading_hash": "#1 优先级：竞品对比",
    "inline_hash": "对标 A/B # 不含定价页",
    "double_quotes": '用户说"帮我看看竞品"时匹配',
    "single_quotes": "用户说'对标一下'时匹配",
    "newline": "第一行说明\n第二行补充",
    "yaml_keywords": "null true false ~ - [] {} @ ` |",
    "at_cap": "调研" * 100,
}


@pytest.mark.parametrize("label", list(_DESCRIPTIONS), ids=list(_DESCRIPTIONS))
def test_description_survives_round_trip(tmp_path, label):
    description = _DESCRIPTIONS[label]
    store = PlaybookStore(tmp_path)
    spec = _spec(description)
    store.save(spec)

    loaded = store.load(spec.name)
    assert loaded.description == description, label
    # The block region has to come back intact too: a frontmatter that fails to
    # terminate would swallow the body and the fenced block with it.
    assert loaded.nodes is not None and [n.id for n in loaded.nodes] == ["scan"]
    assert loaded.triggers.keywords == ["竞品"]
    assert loaded.params["target"].required is True


def test_saved_file_keeps_the_three_regions(tmp_path):
    store = PlaybookStore(tmp_path)
    store.save(_spec("触发场景：竞品对比"))

    text = (tmp_path / "competitor-scan" / "playbook.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert text.count("---\n") >= 2
    assert "```yaml playbook-spec\n" in text
    # The human region names the playbook, so a reader who opens the file sees
    # what it is before any machine field.
    assert "# competitor-scan" in text
