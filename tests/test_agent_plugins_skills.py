"""The Agent Plugin skill rules: validation, discovery and import.

A package is data only, so every rule here is about what may enter the
workspace skill tree: a skill must satisfy the Agent Skills rules, it must
live at the fixed ``skills/`` location, and the copy Raven takes is the copy
that counts -- including the always-injection flag, which a third-party
package does not get.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from raven.agent_plugins import skills as skills_module
from raven.agent_plugins.ledger import LedgerEntry, PackageLedger
from raven.agent_plugins.package import load_package
from raven.agent_plugins.skills import (
    PackageSkill,
    discover_skills,
    drop_always_flag,
    import_package,
    strip_always_keys,
    validate_skill,
)

SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PACKAGE_NAME = "demo-pack"


def _package_root(tmp_path: Path, name: str = PACKAGE_NAME) -> Path:
    """A package root holding a valid ``plugin.json``."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": SCHEMA_URL, "name": name, "version": "1.0.0"}),
        encoding="utf-8",
    )
    return root


def _frontmatter(name: str, description: str = "A demo skill.") -> str:
    return f"name: {name}\ndescription: {description}\n"


def _write_skill(directory: Path, frontmatter: str, body: str = "Do the thing.\n") -> Path:
    """A skill directory holding a SKILL.md with the given frontmatter."""
    directory.mkdir(parents=True, exist_ok=True)
    if frontmatter and not frontmatter.endswith("\n"):
        frontmatter += "\n"
    path = directory / "SKILL.md"
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


def _write_skill_without_frontmatter(directory: Path) -> Path:
    """A skill directory whose SKILL.md carries no frontmatter at all."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text("# Alpha\n\nNo frontmatter here.\n", encoding="utf-8")
    return path


def _link_directory(target: Path, link: Path) -> None:
    """A directory link pointing at *target* (a symlink, or a junction on Windows)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        pytest.skip("this platform cannot create a directory link")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(
            "cannot create a directory link: "
            + result.stdout.decode("utf-8", "replace").strip()
            + " "
            + result.stderr.decode("utf-8", "replace").strip()
        )


def _workspace(tmp_path: Path) -> tuple[Path, PackageLedger]:
    workspace = tmp_path / "workspace"
    return workspace, PackageLedger(workspace / "ledger.json")


# ---------------------------------------------------------------------------
# validate_skill
# ---------------------------------------------------------------------------


def test_validate_skill_accepts_a_well_formed_skill(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    skill_md = _write_skill(directory, _frontmatter("alpha"))

    skill, reason = validate_skill(directory)

    assert reason is None
    assert skill is not None
    assert skill.name == "alpha"
    assert skill.directory == directory
    assert skill.skill_md == skill_md


def test_validate_skill_rejects_a_missing_frontmatter(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_skill_without_frontmatter(directory)

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_missing_name(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_skill(directory, "description: A demo skill.\n")

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_an_uppercase_name(tmp_path: Path) -> None:
    directory = tmp_path / "Alpha"
    _write_skill(directory, _frontmatter("Alpha"))

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_name_with_a_double_hyphen(tmp_path: Path) -> None:
    directory = tmp_path / "alpha--beta"
    _write_skill(directory, _frontmatter("alpha--beta"))

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_name_longer_than_64_characters(tmp_path: Path) -> None:
    long_name = "a" * 65
    directory = tmp_path / long_name
    _write_skill(directory, _frontmatter(long_name))

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_name_that_differs_from_the_directory(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_skill(directory, _frontmatter("beta"))

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_missing_description(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_skill(directory, "name: alpha\n")

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


def test_validate_skill_rejects_a_description_longer_than_1024_characters(tmp_path: Path) -> None:
    directory = tmp_path / "alpha"
    _write_skill(directory, _frontmatter("alpha", "x" * 1025))

    skill, reason = validate_skill(directory)

    assert skill is None
    assert reason


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------


def test_discover_skills_without_a_skills_directory_finds_nothing(tmp_path: Path) -> None:
    package = load_package(_package_root(tmp_path))

    skills, problems = discover_skills(package)

    assert skills == []
    assert problems == []


def test_discover_skills_reports_a_skills_directory_that_is_a_file(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    (root / "skills").write_text("not a directory\n", encoding="utf-8")
    package = load_package(root)

    skills, problems = discover_skills(package)

    assert skills == []
    assert len(problems) == 1
    assert "not a directory" in problems[0].reason


def test_discover_skills_finds_only_the_immediate_children(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    _write_skill(root / "skills" / "group" / "nested", _frontmatter("nested"))
    package = load_package(root)

    skills, problems = discover_skills(package)

    assert [skill.name for skill in skills] == ["alpha"]
    assert problems == []


def test_discover_skills_ignores_a_directory_without_a_skill_md(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    (root / "skills" / "empty").mkdir(parents=True)
    package = load_package(root)

    skills, problems = discover_skills(package)

    assert skills == []
    assert problems == []


def test_discover_skills_refuses_a_skill_resolving_outside_the_package(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    outside = tmp_path / "outside" / "beta"
    _write_skill(outside, _frontmatter("beta"))
    _link_directory(outside, root / "skills" / "beta")
    package = load_package(root)

    skills, problems = discover_skills(package)

    assert skills == []
    assert len(problems) == 1
    assert "outside the package root" in problems[0].reason


# ---------------------------------------------------------------------------
# import_package
# ---------------------------------------------------------------------------


def test_import_package_copies_a_skill_into_the_workspace(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    scripts = root / "skills" / "alpha" / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('hello')\n", encoding="utf-8")
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    target = workspace / "skills" / PACKAGE_NAME / "alpha"
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "run.py").read_text(encoding="utf-8") == "print('hello')\n"
    assert report.package == PACKAGE_NAME
    assert report.version == "1.0.0"
    assert report.target == str(workspace / "skills" / PACKAGE_NAME)
    assert report.installed == ("alpha",)
    assert report.failed == ()


def test_import_package_strips_the_always_flag_from_the_copy(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    source = _write_skill(
        root / "skills" / "alpha",
        _frontmatter("alpha") + "always: true\n",
    )
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    copied = (workspace / "skills" / PACKAGE_NAME / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert report.installed == ("alpha",)
    assert "always:" not in copied
    assert "name: alpha" in copied
    assert "description: A demo skill." in copied
    assert "always: true" in source.read_text(encoding="utf-8")


def test_import_package_strips_a_metadata_always_entry(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(
        root / "skills" / "alpha",
        _frontmatter("alpha") + "metadata:\n  raven:\n    always: true\n",
    )
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    copied = (workspace / "skills" / PACKAGE_NAME / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert report.installed == ("alpha",)
    assert "always" not in copied
    assert "metadata:" in copied
    assert "raven:" in copied


def test_import_package_skips_a_blocked_skill(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger, blocklist=["alpha"])

    assert report.installed == ()
    skipped = dict(report.skipped)
    assert "alpha" in skipped
    assert "blocklist" in skipped["alpha"]
    assert not (workspace / "skills" / PACKAGE_NAME / "alpha").exists()


def test_import_package_matches_the_blocklist_case_insensitively(tmp_path: Path) -> None:
    """The blocklist is matched the way the Skill Hub matches it."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger, blocklist=["Alpha"])

    assert report.installed == ()
    skipped = dict(report.skipped)
    assert "alpha" in skipped
    assert not (workspace / "skills" / PACKAGE_NAME / "alpha").exists()


def test_import_package_never_overwrites_a_directory_the_ledger_does_not_claim(
    tmp_path: Path,
) -> None:
    """A skill the operator put inside the package directory is left alone."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "beta", _frontmatter("beta"))
    workspace, ledger = _workspace(tmp_path)
    ledger.record(
        LedgerEntry(
            name=PACKAGE_NAME,
            version="1.0.0",
            source=str(root),
            skills=("alpha",),
        )
    )
    mine = workspace / "skills" / PACKAGE_NAME / "beta"
    _write_skill(mine, _frontmatter("beta"), body="The operator's own copy.\n")

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ()
    skipped = dict(report.skipped)
    assert "beta" in skipped
    assert "ledger" in skipped["beta"]
    assert "The operator's own copy." in (mine / "SKILL.md").read_text(encoding="utf-8")


def test_import_package_replaces_a_skill_the_ledger_claims(tmp_path: Path) -> None:
    """A skill the ledger installed is the ledger's to replace."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "beta", _frontmatter("beta"), body="The new copy.\n")
    workspace, ledger = _workspace(tmp_path)
    ledger.record(
        LedgerEntry(
            name=PACKAGE_NAME,
            version="1.0.0",
            source=str(root),
            skills=("beta",),
        )
    )
    mine = workspace / "skills" / PACKAGE_NAME / "beta"
    _write_skill(mine, _frontmatter("beta"), body="The old copy.\n")

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ("beta",)
    assert "The new copy." in (mine / "SKILL.md").read_text(encoding="utf-8")


def test_import_package_refuses_a_package_directory_the_ledger_does_not_own(
    tmp_path: Path,
) -> None:
    """A package directory that is not the ledger's is never written into."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)
    mine = workspace / "skills" / PACKAGE_NAME
    mine.mkdir(parents=True)
    (mine / "NOTES.md").write_text("The operator's own directory.\n", encoding="utf-8")

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ()
    assert report.refusal
    assert "ledger" in report.refusal
    assert (mine / "NOTES.md").is_file()


def test_import_package_skips_a_skill_naming_another_products_home(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(
        root / "skills" / "alpha",
        _frontmatter("alpha"),
        body="Read the settings at ~/.openclaw/config.json first.\n",
    )
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ()
    skipped = dict(report.skipped)
    assert "alpha" in skipped
    assert "openclaw" in skipped["alpha"]
    assert not (workspace / "skills" / PACKAGE_NAME / "alpha").exists()


def test_import_package_never_overwrites_a_directory_it_does_not_own(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)
    target = workspace / "skills" / PACKAGE_NAME / "alpha"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("mine\n", encoding="utf-8")

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ()
    # The collision is reported either as a refusal of the whole package or
    # as a skip of the one skill; what matters is that it is reported and
    # that the directory on disk is left alone.
    reasons = " ".join([report.refusal, *(reason for _, reason in report.skipped)])
    assert "not owned" in reasons
    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine\n"
    assert not (target / "SKILL.md").exists()


def test_import_package_replaces_a_directory_the_ledger_owns(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)
    package = load_package(root)

    first = import_package(package, workspace=workspace, ledger=ledger)
    assert first.installed == ("alpha",)
    ledger.record(LedgerEntry(name=PACKAGE_NAME, version="1.0.0", skills=("alpha",)))

    target = workspace / "skills" / PACKAGE_NAME / "alpha"
    (target / "stale.txt").write_text("old\n", encoding="utf-8")
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha", "A newer demo skill."))

    second = import_package(package, workspace=workspace, ledger=ledger)

    assert second.installed == ("alpha",)
    assert not (target / "stale.txt").exists()
    assert "A newer demo skill." in (target / "SKILL.md").read_text(encoding="utf-8")


def test_import_package_skips_the_second_skill_claiming_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _package_root(tmp_path)
    first = _write_skill(root / "skills" / "alpha", _frontmatter("alpha", "The first copy."))
    second = _write_skill(root / "skills" / "beta", _frontmatter("beta", "The second copy."))
    monkeypatch.setattr(
        skills_module,
        "discover_skills",
        lambda package: (
            [
                PackageSkill(name="alpha", directory=first.parent, skill_md=first),
                PackageSkill(name="alpha", directory=second.parent, skill_md=second),
            ],
            [],
        ),
    )
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ("alpha",)
    skipped = dict(report.skipped)
    assert "alpha" in skipped
    assert "already claims the name" in skipped["alpha"]


def test_import_package_reports_a_skill_whose_name_mismatches_its_directory(tmp_path: Path) -> None:
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    _write_skill(root / "skills" / "beta", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ("alpha",)
    assert len(report.problems) == 1
    assert report.problems[0].name == "beta"


# ---------------------------------------------------------------------------
# strip_always_keys / drop_always_flag
# ---------------------------------------------------------------------------


def test_strip_always_keys_removes_a_top_level_always_line() -> None:
    block = _frontmatter("alpha") + "always: true\n"

    stripped = strip_always_keys(block)

    assert "always" not in stripped
    assert "name: alpha" in stripped
    assert "description: A demo skill." in stripped


def test_strip_always_keys_removes_always_inside_a_metadata_block() -> None:
    block = _frontmatter("alpha") + "metadata:\n  raven:\n    always: true\n"

    stripped = strip_always_keys(block)

    assert "always" not in stripped
    assert "metadata:" in stripped
    assert "raven:" in stripped


def test_strip_always_keys_leaves_the_other_keys_untouched() -> None:
    block = _frontmatter("alpha") + "version: 2\n"

    assert strip_always_keys(block) == block


def test_strip_always_keys_leaves_a_similar_key_untouched() -> None:
    block = _frontmatter("alpha") + "always: true\nalways_enabled: true\n"

    stripped = strip_always_keys(block)

    assert "always_enabled: true" in stripped
    assert "\nalways: true" not in stripped


def test_drop_always_flag_leaves_a_file_without_frontmatter_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    raw = b"# Alpha\n\nNo frontmatter here.\n"
    path.write_bytes(raw)

    drop_always_flag(path)

    assert path.read_bytes() == raw


def test_drop_always_flag_removes_the_flag_from_a_file_with_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(
        "---\nname: alpha\ndescription: A demo skill.\nalways: true\n---\n\nDo the thing.\n",
        encoding="utf-8",
    )

    drop_always_flag(path)

    text = path.read_text(encoding="utf-8")
    assert "always" not in text
    assert "name: alpha" in text
    assert "description: A demo skill." in text
    assert "Do the thing." in text


def test_import_package_refuses_a_skill_whose_file_links_outside_the_package(
    tmp_path: Path,
) -> None:
    """Checking the skill directory is not enough: a file one level down is a path too."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET-FROM-OUTSIDE\n", encoding="utf-8")
    _link_directory(outside, root / "skills" / "alpha" / "assets")
    workspace, ledger = _workspace(tmp_path)

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.installed == ()
    skipped = dict(report.skipped)
    assert "alpha" in skipped
    assert "outside the package root" in skipped["alpha"]
    assert not (workspace / "skills" / PACKAGE_NAME / "alpha" / "assets" / "secret.txt").exists()


def test_import_package_removes_a_skill_the_package_no_longer_ships(tmp_path: Path) -> None:
    """A refresh converges on the package: what it dropped is dropped here too."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    _write_skill(root / "skills" / "beta", _frontmatter("beta"))
    workspace, ledger = _workspace(tmp_path)

    first = import_package(load_package(root), workspace=workspace, ledger=ledger)
    assert first.installed == ("alpha", "beta")
    ledger.record(LedgerEntry(name=PACKAGE_NAME, version="1.0.0", skills=first.installed))

    shutil.rmtree(root / "skills" / "beta")
    second = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert second.installed == ("alpha",)
    assert second.removed == ("beta",)
    assert not (workspace / "skills" / PACKAGE_NAME / "beta").exists()


def test_import_package_keeps_ownership_when_a_stale_skill_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed deletion must not report success or drop the ledger's claim.

    The ledger is the only record of what this package put in the workspace.
    Rewriting it to the installed set after a deletion that did not happen
    leaves the directory unowned, so no later ``remove`` can clear it.
    """
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    _write_skill(root / "skills" / "beta", _frontmatter("beta"))
    workspace, ledger = _workspace(tmp_path)

    first = import_package(load_package(root), workspace=workspace, ledger=ledger)
    ledger.record(LedgerEntry(name=PACKAGE_NAME, version="1.0.0", skills=first.installed))
    shutil.rmtree(root / "skills" / "beta")

    real_rmtree = shutil.rmtree

    def _refuse(path, *args, **kwargs):
        if Path(path).name == "beta":
            raise OSError("the directory is busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skills_module.shutil, "rmtree", _refuse)

    second = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert second.installed == ("alpha",)
    assert second.removed == ()
    assert second.retained == ("beta",)
    assert "busy" in dict(second.failed)["beta"]
    assert (workspace / "skills" / PACKAGE_NAME / "beta").is_dir()


def test_import_package_treats_a_vanished_stale_skill_as_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise after the directory is gone is a success, not a retained claim."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    _write_skill(root / "skills" / "beta", _frontmatter("beta"))
    workspace, ledger = _workspace(tmp_path)

    first = import_package(load_package(root), workspace=workspace, ledger=ledger)
    ledger.record(LedgerEntry(name=PACKAGE_NAME, version="1.0.0", skills=first.installed))
    shutil.rmtree(root / "skills" / "beta")

    real_rmtree = shutil.rmtree

    def _remove_then_raise(path, *args, **kwargs):
        result = real_rmtree(path, *args, **kwargs)
        if Path(path).name == "beta":
            raise OSError("the directory vanished while being removed")
        return result

    monkeypatch.setattr(skills_module.shutil, "rmtree", _remove_then_raise)

    second = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert second.removed == ("beta",)
    assert second.retained == ()
    assert second.failed == ()
    assert not (workspace / "skills" / PACKAGE_NAME / "beta").exists()


def test_import_package_keeps_a_directory_the_ledger_never_claimed(tmp_path: Path) -> None:
    """The stale sweep is scoped to the ledger, not to the package directory."""
    root = _package_root(tmp_path)
    _write_skill(root / "skills" / "alpha", _frontmatter("alpha"))
    workspace, ledger = _workspace(tmp_path)
    ledger.record(LedgerEntry(name=PACKAGE_NAME, version="1.0.0", skills=("alpha",)))
    foreign = workspace / "skills" / PACKAGE_NAME / "notes"
    foreign.mkdir(parents=True)
    (foreign / "KEEP.txt").write_text("not ours\n", encoding="utf-8")

    report = import_package(load_package(root), workspace=workspace, ledger=ledger)

    assert report.removed == ()
    assert (foreign / "KEEP.txt").is_file()
