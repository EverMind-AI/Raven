from __future__ import annotations

import re
from pathlib import Path

from raven.memory_engine.skill_local.registry import SkillRegistry
from raven_design.selector import VISUAL_DOMAIN_SKILL_NAMES

SELECTABLE_DOMAIN_SKILLS = VISUAL_DOMAIN_SKILL_NAMES
PRIMARY_DOMAIN_SKILLS = VISUAL_DOMAIN_SKILL_NAMES[:-1]

WRITE_PROOF_SKILLS = (
    "design-brand-identities",
    "create-illustrations-and-scenes",
    "create-marketing-graphics",
    "design-editorial-and-presentations",
    "create-technical-diagrams",
    "build-games-and-playful-experiences",
)

LOCAL_REFERENCE_RE = re.compile(r"\]\((references/[^)#]+)(?:#[^)]+)?\)")
SKILL_REFERENCE_RE = re.compile(r"\$([a-z][a-z0-9-]+)")


def _registry(tmp_path: Path) -> SkillRegistry:
    from raven_design.plugin.hook import packaged_skills_dir

    return SkillRegistry(
        tmp_path / "workspace",
        builtin_skills_dir=tmp_path / "no-builtin",
        extra_dirs=[(packaged_skills_dir(), "design-engine", True)],
    )


def test_visual_domain_skills_are_packaged_with_resolvable_references(
    tmp_path: Path,
) -> None:
    by_name = {meta.name: meta for meta in _registry(tmp_path).list_all()}

    assert len(SELECTABLE_DOMAIN_SKILLS) == 15
    assert set(SELECTABLE_DOMAIN_SKILLS) <= by_name.keys()
    for name in SELECTABLE_DOMAIN_SKILLS:
        assert by_name[name].source == "design-engine"
        assert by_name[name].always is False
    assert by_name["visual-artifact-design"].source == "design-engine"
    assert by_name["visual-artifact-design"].always is True
    assert "visual-artifact-design" not in SELECTABLE_DOMAIN_SKILLS
    assert "weather" not in SELECTABLE_DOMAIN_SKILLS

    for name in PRIMARY_DOMAIN_SKILLS:
        meta = by_name[name]
        skill_dir = meta.path.parent
        skill_text = meta.path.read_text(encoding="utf-8")
        patterns_path = skill_dir / "references" / "patterns.md"

        assert meta.source == "design-engine"
        assert meta.always is False
        assert skill_dir.name == name
        assert patterns_path.is_file()
        assert patterns_path.read_text(encoding="utf-8").strip()

        local_references = LOCAL_REFERENCE_RE.findall(skill_text)
        assert local_references
        for relative_path in local_references:
            assert (skill_dir / relative_path).is_file()

        combined_text = skill_text + patterns_path.read_text(encoding="utf-8")
        for referenced_skill in SKILL_REFERENCE_RE.findall(combined_text):
            assert referenced_skill in by_name


def test_domains_keep_consumer_specific_visual_contracts(tmp_path: Path) -> None:
    by_name = {meta.name: meta for meta in _registry(tmp_path).list_all()}

    for name in PRIMARY_DOMAIN_SKILLS:
        text = by_name[name].path.read_text(encoding="utf-8")
        assert any(marker in text for marker in ("最终消费者", "真实消费者", "读者任务", "身份任务")), name
        assert any(
            marker in text
            for marker in (
                "介质中立",
                "目标媒介",
                "每种合同媒介在自己的最终消费者中验证",
                "典型与最窄合同宽度",
                "每种媒介在自己的最终消费者中验证",
            )
        ), name
        assert "来源" in text and "许可" in text, name
        assert any(marker in text for marker in ("最终像素", "最终验收", "目标环境验收", "代表终态")), name
        assert "$build-polished-visual-frontends" in text, name


def test_browser_only_text_audits_have_non_web_evidence_paths(tmp_path: Path) -> None:
    by_name = {meta.name: meta for meta in _registry(tmp_path).list_all()}

    for name in ("design-icons-and-symbols", "build-ui-components-and-systems"):
        meta = by_name[name]
        text = meta.path.read_text(encoding="utf-8")
        text += (meta.path.parent / "references" / "patterns.md").read_text(encoding="utf-8")
        assert "浏览器 DOM/SVG" in text, name
        assert "computed-style" in text, name
        assert "目标 renderer" in text, name
        assert "实体样张" in text, name


def test_diagnose_audit_stays_read_only_for_write_capable_domains(tmp_path: Path) -> None:
    by_name = {meta.name: meta for meta in _registry(tmp_path).list_all()}
    shared_text = by_name["visual-artifact-design"].path.read_text(encoding="utf-8")

    assert "Diagnose/Audit 默认只读" in shared_text
    assert "不建立制作阶段" in shared_text
    assert "不为取得更好看的证据修改源文件" in shared_text

    for name in WRITE_PROOF_SKILLS:
        text = by_name[name].path.read_text(encoding="utf-8")
        if "Diagnose/Audit 一律零写入" in text:
            assert "Create/Edit" in text, name
            assert "一次性验证副本" in text, name
            assert "hash" in text, name
        else:
            assert "$visual-artifact-design" in text, name


def test_shared_review_gates_are_scope_driven_not_fixed_rounds(tmp_path: Path) -> None:
    meta = _registry(tmp_path).get("build-polished-visual-frontends")
    assert meta is not None
    skill_text = meta.path.read_text(encoding="utf-8")
    review_text = (meta.path.parent / "references" / "anti-slop-review.md").read_text(encoding="utf-8")

    assert "视觉判断不是固定三轮" in skill_text
    assert "至少完成三次视觉判断" not in skill_text
    assert "## 2. 三轮审查" not in review_text
    for marker in ("方向检查", "系统检查", "最终检查", "Diagnose/Audit"):
        assert marker in skill_text
    assert "按风险触发审查门槛" in review_text


def test_domain_scope_and_accessibility_contracts(tmp_path: Path) -> None:
    by_name = {meta.name: meta for meta in _registry(tmp_path).list_all()}

    brand = by_name["design-brand-identities"].path.read_text(encoding="utf-8")
    assert "支撑层剥离" in brand and "回退鲁棒性" in brand
    assert "`N/A` 只表示已接受范围外" in brand

    ui = by_name["build-ui-components-and-systems"].path.read_text(encoding="utf-8")
    assert "greenfield" in ui and "只检查和修改受影响类别" in ui

    data = by_name["create-data-visualizations"].path.read_text(encoding="utf-8")
    assert "精确值入口或语义表" in data
    assert "键盘焦点和 hover 在交互合同中提供等价信息" in data

    games_meta = by_name["build-games-and-playful-experiences"]
    games = games_meta.path.read_text(encoding="utf-8")
    games += (games_meta.path.parent / "references" / "patterns.md").read_text(encoding="utf-8")
    assert "音频启用/解码仅在合同包含声音时检查" in games
    assert "存档/迁移仅在承诺持久化时检查" in games
    assert "离线边界仅在承诺离线运行时检查" in games
