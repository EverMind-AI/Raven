"""Hermes skill origin classification and installation."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from raven.importer.skills import DiscoveredSkill, SkillOrigin, package_hash


def _skill(
    root: Path, name: str, *, body: str = "---\nname: {n}\n---\nbody\n", extra: dict[str, str] | None = None
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body.format(n=name), encoding="utf-8")
    for rel, text in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def test_package_hash_covers_attachments(tmp_path: Path) -> None:
    a = _skill(tmp_path / "a", "s", extra={"references/x.md": "one"})
    b = _skill(tmp_path / "b", "s", extra={"references/x.md": "two"})
    assert package_hash(a) != package_hash(b)


def test_package_hash_is_order_independent(tmp_path: Path) -> None:
    a = _skill(tmp_path / "a", "s", extra={"references/x.md": "1", "scripts/y.sh": "2"})
    b = _skill(tmp_path / "b", "s", extra={"scripts/y.sh": "2", "references/x.md": "1"})
    assert package_hash(a) == package_hash(b)


def test_package_hash_stable_across_calls(tmp_path: Path) -> None:
    d = _skill(tmp_path, "s")
    assert package_hash(d) == package_hash(d)


# Golden digest: cross-checked equal to Hermes' own _dir_hash over all 82 skill
# directories of a real install, and equal to all 70 md5 values Hermes records in
# skills/.bundled_manifest. Hermes is not a dependency, so this constant is the only
# way a test can catch the algorithm drifting away from the format it must match.
def test_package_hash_matches_hermes_digest(tmp_path: Path) -> None:
    d = _skill(tmp_path, "golden", body="---\nname: golden\n---\nbody\n", extra={"references/a.md": "attachment\n"})
    assert package_hash(d) == "1021154a6d61e36612c215e0297cac4b"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_unreadable_file_truncates_rather_than_raising(tmp_path: Path) -> None:
    blocked_dir = _skill(tmp_path / "blocked", "s", extra={"references/x.md": "one"})
    readable_dir = _skill(tmp_path / "readable", "s", extra={"references/x.md": "one"})
    (blocked_dir / "references" / "x.md").chmod(0o000)
    try:
        blocked = package_hash(blocked_dir)
    finally:
        (blocked_dir / "references" / "x.md").chmod(0o644)
    assert blocked != package_hash(readable_dir)
    assert package_hash(blocked_dir) == package_hash(readable_dir)


def test_discovered_skill_is_frozen(tmp_path: Path) -> None:
    s = DiscoveredSkill(name="s", path=tmp_path, origin=SkillOrigin.AGENT_CREATED, size=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.name = "other"  # type: ignore[misc]
