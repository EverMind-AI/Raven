"""Hermes skill origin classification and installation."""

from __future__ import annotations

import dataclasses
import errno
import json
import os
from pathlib import Path

import pytest

from raven.importer.skills import DiscoveredSkill, SkillOrigin, package_hash
from raven.importer.skills.hermes import HermesSkillSource


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


def _home_with_skills(
    tmp_path: Path,
    *,
    manifest: dict[str, str] | None = None,
    hub: list[str] | None = None,
    usage: dict[str, dict[str, str]] | None = None,
    skills: tuple[tuple[str, str], ...] = (),
) -> tuple[Path, dict[str, Path]]:
    home = tmp_path / ".hermes"
    sk = home / "skills"
    sk.mkdir(parents=True)
    made: dict[str, Path] = {}
    for category, name in skills:
        parent = sk / category if category else sk
        made[name] = _skill(parent, name)
    if manifest is not None:
        lines = []
        for name, value in manifest.items():
            digest = package_hash(made[name]) if value == "PRISTINE" else value
            lines.append(f"{name}:{digest}")
        (sk / ".bundled_manifest").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if hub is not None:
        (sk / ".hub").mkdir()
        (sk / ".hub" / "lock.json").write_text(
            json.dumps({"version": 1, "installed": {n: {} for n in hub}}), encoding="utf-8"
        )
    if usage is not None:
        (sk / ".usage.json").write_text(json.dumps(usage), encoding="utf-8")
    return home, made


async def test_all_five_origins_classified(tmp_path: Path) -> None:
    home, made = _home_with_skills(
        tmp_path,
        skills=(("cat", "pristine"), ("cat", "modified"), ("", "fromhub"), ("", "byagent"), ("", "mystery")),
        hub=["fromhub"],
        usage={"byagent": {"created_by": "agent"}},
    )
    (home / "skills" / ".bundled_manifest").write_text(
        f"pristine:{package_hash(made['pristine'])}\nmodified:deadbeef\n", encoding="utf-8"
    )
    got = {s.name: s.origin for s in await HermesSkillSource(hermes_home=home).discover()}
    assert got == {
        "pristine": SkillOrigin.BUNDLED_PRISTINE,
        "modified": SkillOrigin.BUNDLED_MODIFIED,
        "fromhub": SkillOrigin.HUB_INSTALLED,
        "byagent": SkillOrigin.AGENT_CREATED,
        "mystery": SkillOrigin.LOCAL_UNKNOWN,
    }


async def test_missing_manifest_makes_everything_importable(tmp_path: Path) -> None:
    home, _ = _home_with_skills(tmp_path, skills=(("cat", "a"), ("", "b")))
    got = {s.origin for s in await HermesSkillSource(hermes_home=home).discover()}
    assert got == {SkillOrigin.LOCAL_UNKNOWN}


async def test_v1_manifest_without_hashes_counts_as_modified(tmp_path: Path) -> None:
    home, _ = _home_with_skills(tmp_path, skills=(("cat", "a"),))
    (home / "skills" / ".bundled_manifest").write_text("a\n", encoding="utf-8")
    (skill,) = await HermesSkillSource(hermes_home=home).discover()
    assert skill.origin is SkillOrigin.BUNDLED_MODIFIED


async def test_manifest_key_matched_by_frontmatter_name(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    sk = home / "skills" / "cat"
    sk.mkdir(parents=True)
    d = sk / "dir-name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: display-name\n---\nbody\n", encoding="utf-8")
    (home / "skills" / ".bundled_manifest").write_text(f"display-name:{package_hash(d)}\n", encoding="utf-8")
    (skill,) = await HermesSkillSource(hermes_home=home).discover()
    assert skill.origin is SkillOrigin.BUNDLED_PRISTINE


async def test_frontmatter_name_wins_over_a_colliding_directory_name(tmp_path: Path) -> None:
    """Hermes writes manifest keys frontmatter-name-first, dirname as fallback.

    A directory named after one skill whose frontmatter names another must be
    compared against the manifest entry keyed on its own frontmatter name, not
    against a same-named directory belonging to an unrelated skill.
    """
    home = tmp_path / ".hermes"
    sk = home / "skills"
    sk.mkdir(parents=True)
    (sk / "other-dir").mkdir()
    (sk / "other-dir" / "SKILL.md").write_text("---\nname: shared-name\n---\nbody\n", encoding="utf-8")
    real = sk / "shared-name"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: real-frontmatter\n---\nbody\n", encoding="utf-8")
    (sk / ".bundled_manifest").write_text(
        f"shared-name:{package_hash(sk / 'other-dir')}\nreal-frontmatter:{package_hash(real)}\n",
        encoding="utf-8",
    )
    got = {s.name: s.origin for s in await HermesSkillSource(hermes_home=home).discover()}
    assert got == {
        "other-dir": SkillOrigin.BUNDLED_PRISTINE,
        "shared-name": SkillOrigin.BUNDLED_PRISTINE,
    }


async def test_corrupt_usage_json_degrades_to_unknown(tmp_path: Path) -> None:
    home, _ = _home_with_skills(tmp_path, skills=(("", "a"),))
    (home / "skills" / ".usage.json").write_text("{not json", encoding="utf-8")
    (skill,) = await HermesSkillSource(hermes_home=home).discover()
    assert skill.origin is SkillOrigin.LOCAL_UNKNOWN


async def test_hub_dir_is_not_scanned_as_a_skill(tmp_path: Path) -> None:
    home, _ = _home_with_skills(tmp_path, skills=(("", "a"),), hub=["a"])
    hub_skill_dir = home / "skills" / ".hub" / "cache" / "something"
    hub_skill_dir.mkdir(parents=True)
    (hub_skill_dir / "SKILL.md").write_text("---\nname: something\n---\nbody\n", encoding="utf-8")
    names = {s.name for s in await HermesSkillSource(hermes_home=home).discover()}
    assert names == {"a"}


async def test_manifest_with_crlf_and_trailing_space_still_matches(tmp_path: Path) -> None:
    home, made = _home_with_skills(tmp_path, skills=(("", "a"),))
    digest = package_hash(made["a"])
    (home / "skills" / ".bundled_manifest").write_bytes(f"a : {digest} \r\n".encode())
    (skill,) = await HermesSkillSource(hermes_home=home).discover()
    assert skill.origin is SkillOrigin.BUNDLED_PRISTINE


async def test_file_vanishing_mid_size_scan_does_not_abort_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dangling symlink cannot express this: is_file() returns False for one
    without raising, and it does not route through Path.stat, so only a failing
    stat() on a file is_file() just accepted -- one deleted mid-scan -- reaches
    the guard."""
    home, _ = _home_with_skills(tmp_path, skills=(("", "a"), ("", "b")))
    real_stat = Path.stat

    def flaky_stat(self: Path, **kwargs: object) -> os.stat_result:
        if self.name == "SKILL.md" and self.parent.name == "a":
            raise OSError(errno.ENOENT, "vanished mid-scan")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    sizes = {s.name: s.size for s in await HermesSkillSource(hermes_home=home).discover()}
    assert set(sizes) == {"a", "b"}
    assert sizes["a"] == 0
    assert sizes["b"] > 0


async def test_archived_skills_are_not_discovered(tmp_path: Path) -> None:
    """The curator retires a skill by moving it under .archive; importing it
    would resurrect exactly what the user chose to drop."""
    home, _ = _home_with_skills(tmp_path, skills=(("", "live"),))
    _skill(home / "skills" / ".archive", "retired")
    names = {s.name for s in await HermesSkillSource(hermes_home=home).discover()}
    assert names == {"live"}


async def test_documentation_package_under_a_skill_is_not_a_skill(tmp_path: Path) -> None:
    home, made = _home_with_skills(tmp_path, skills=(("", "live"),))
    _skill(made["live"] / "references", "old-version")
    names = {s.name for s in await HermesSkillSource(hermes_home=home).discover()}
    assert names == {"live"}


async def test_a_category_named_like_a_support_dir_stays_discoverable(tmp_path: Path) -> None:
    home, _ = _home_with_skills(tmp_path, skills=(("scripts", "real-skill"),))
    names = {s.name for s in await HermesSkillSource(hermes_home=home).discover()}
    assert names == {"real-skill"}


async def test_missing_skills_dir_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / ".hermes").mkdir()
    assert await HermesSkillSource(hermes_home=tmp_path / ".hermes").discover() == []
