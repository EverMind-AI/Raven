"""always_max must cap only inject: full bodies and never drop description digests."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.memory_engine.skill_forge import LocalSkillCatalog


def _skill(root: Path, name: str, *, digest: bool = False) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    meta = '{"raven":{"always":true,"inject":"description"}}' if digest else '{"raven":{"always":true}}'
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc {name}\nmetadata: {meta}\n---\n\nBody {name}.\n",
        encoding="utf-8",
    )


def _catalog(tmp_path: Path, digests: int, fulls: int, always_max: int) -> LocalSkillCatalog:
    ws = tmp_path / "ws"
    skills = ws / "skills"
    for i in range(digests):
        _skill(skills, f"dig{i:02d}", digest=True)
    for i in range(fulls):
        _skill(skills, f"full{i:02d}")
    (tmp_path / "builtin").mkdir(exist_ok=True)
    cfg = SimpleNamespace(always_max=always_max, disable_always=False)
    return LocalSkillCatalog(ws, config=cfg, builtin_skills_dir=tmp_path / "builtin", start_watcher=False)


def test_digests_survive_always_max_and_dropped_full_skills_are_named(tmp_path, caplog):
    cat = _catalog(tmp_path, digests=3, fulls=4, always_max=2)
    with caplog.at_level(logging.WARNING):
        names = [m.name for m in cat.get_always_skills()]
    assert {"dig00", "dig01", "dig02"} <= set(names)
    assert sum(n.startswith("full") for n in names) == 2
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "full02" in warned and "full03" in warned
    assert "full00" not in warned and "dig" not in warned


def test_no_warning_when_nothing_truncated(tmp_path, caplog):
    cat = _catalog(tmp_path, digests=5, fulls=2, always_max=2)
    with caplog.at_level(logging.WARNING):
        assert len(cat.get_always_skills()) == 7
    assert not caplog.records


def test_twelve_always_skills_zero_silent_truncation(tmp_path, caplog):
    cat = _catalog(tmp_path, digests=12, fulls=0, always_max=5)
    with caplog.at_level(logging.WARNING):
        metas = cat.get_always_skills()
        block = cat.load_always_block(metas, max_inject=5)
    assert len(metas) == 12
    assert all(f"### Skill: dig{i:02d}" in block for i in range(12))
    assert not caplog.records


def test_self_surface_exposes_always_max():
    from raven.config import self_surface as surface

    always_max = surface.find("skillForge.alwaysMax")
    assert always_max is not None
    setting_max, bound_max = always_max
    assert bound_max == []
    assert setting_max.kind == "int"
    assert setting_max.low == 1
    assert setting_max.effect is surface.Effect.RELOAD
    assert surface.default_of("skillForge.alwaysMax") == 5
    assert surface.check_value(setting_max, 3) == 3
    with pytest.raises(ValueError):
        surface.check_value(setting_max, 0)
