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


def _golden_skill(root: Path) -> Path:
    """A package whose rglob order differs from its sorted order.

    `z-after-nested.md` sorts after `references/a.md` but rglob yields it before,
    so a digest computed without the sort differs -- which is what makes the
    golden constants able to detect a dropped `sorted()`.
    """
    return _skill(
        root,
        "golden",
        body="---\nname: golden\n---\nbody\n",
        extra={"references/a.md": "attachment\n", "z-after-nested.md": "tail\n"},
    )


# Golden digests: cross-checked equal to Hermes' own _dir_hash on this fixture, on all
# 82 skill directories of a real install, and against all 70 md5 values Hermes records
# in skills/.bundled_manifest. Hermes is not a dependency, so these constants are the
# only way a test can catch the algorithm drifting from the format it must match.
def test_package_hash_matches_hermes_digest(tmp_path: Path) -> None:
    assert package_hash(_golden_skill(tmp_path)) == "61f145ac5c0353877e5e28fe8bf34a01"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 does not block root")
def test_unreadable_file_aborts_the_whole_hash(tmp_path: Path) -> None:
    """Pins abort-on-first-failure, which a per-file `continue` would silently change.

    Hermes' try wraps its whole loop, so a package with one unreadable attachment
    hashes to this value on both sides. A refactor that skipped only the bad file
    would keep the trailing file's bytes and diverge from Hermes for good.
    """
    d = _golden_skill(tmp_path)
    (d / "references" / "a.md").chmod(0o000)
    try:
        assert package_hash(d) == "b18db9797cf3dc5aa332dc908cb98646"
    finally:
        (d / "references" / "a.md").chmod(0o644)


def test_discovered_skill_is_frozen(tmp_path: Path) -> None:
    s = DiscoveredSkill(name="s", path=tmp_path, origin=SkillOrigin.AGENT_CREATED, size=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.name = "other"  # type: ignore[misc]
