"""The skill blocklist is read from the live config on every ask.

A settings switch writes ``skillForge.blocklist`` to disk; the running catalog
must hide and restore the skill on the next ask without being rebuilt.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.config.live import LiveConfig, skill_blocklist
from raven.memory_engine import LocalSkillCatalog


def _write_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\nBody.\n", encoding="utf-8")


def _write_config(path: Path, blocklist: list[str]) -> None:
    path.write_text(json.dumps({"skillForge": {"blocklist": blocklist}}), encoding="utf-8")


def test_blocklist_flipped_on_disk_applies_without_a_rebuild(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills", "codeword")
    _write_skill(workspace / "skills", "other")
    cfg = tmp_path / "config.json"
    _write_config(cfg, [])
    live = LiveConfig(cfg)
    catalog = LocalSkillCatalog(
        workspace,
        builtin_skills_dir=tmp_path / "no-builtin",
        start_watcher=False,
        blocklist_reader=lambda: skill_blocklist(live),
    )

    def names() -> set[str]:
        return {m.name for m in catalog.pool._registry.list_all()}

    assert not catalog._is_blocked("codeword")
    assert names() == {"codeword", "other"}

    _write_config(cfg, ["codeword"])
    assert catalog._is_blocked("codeword")
    assert names() == {"other"}

    _write_config(cfg, [])
    assert not catalog._is_blocked("codeword")
    assert names() == {"codeword", "other"}


def test_without_a_reader_the_config_list_still_applies(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_skill(workspace / "skills", "codeword")

    class Config:
        enabled = False
        blocklist = ["CodeWord"]

    catalog = LocalSkillCatalog(workspace, config=Config(), builtin_skills_dir=tmp_path / "nb", start_watcher=False)
    assert catalog._is_blocked("codeword")


def test_skill_blocklist_reads_both_spellings(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"skill_forge": {"blocklist": ["A"]}, "skillForge": {"blocklist": ["b"]}}), encoding="utf-8"
    )
    assert skill_blocklist(LiveConfig(cfg)) == frozenset({"a", "b"})
