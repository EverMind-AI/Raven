"""The skills of an Agent Plugin package: discovery, validation, import.

The package format discovers skills from the fixed ``skills/`` location and
only from its immediate children (spec §6.1, §7.1); the skill itself must
conform to the Agent Skills specification. A skill that does not is skipped
and reported, and the package's other skills still load.

The import copies a package's skills into the workspace skill tree, one
directory per package, so that the pool mints one source label per package
rather than one per skill. The copy is what Raven owns, and it is what the
ledger claims.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from raven.agent_plugins.ledger import LedgerEntry, PackageLedger
from raven.agent_plugins.package import AgentPluginPackage, is_contained
from raven.skill_hub.policy import is_blocked, lint_external_paths, normalize_blocklist
from raven.utils.skill_meta import nested_metadata, resolve_always
from raven.utils.text import parse_frontmatter

SKILL_FILENAME = "SKILL.md"

RESERVED_SOURCE_LABELS = frozenset({"workspace", "external", "builtin"})
"""The registry labels a package directory must not take (``registry.py`` layers)."""

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024


@dataclass(frozen=True)
class PackageSkill:
    """One skill of a package, already checked against the Agent Skills rules."""

    name: str
    directory: Path
    skill_md: Path


@dataclass(frozen=True)
class SkillProblem:
    """A skill that cannot be used, and why."""

    name: str
    reason: str


@dataclass(frozen=True)
class ImportReport:
    """What one import did."""

    package: str
    version: str = ""
    target: str = ""
    installed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    """Skills an earlier import installed that this package no longer ships."""
    retained: tuple[str, ...] = ()
    """Skills this package no longer ships but whose copy could not be removed.

    The ledger keeps claiming these: the directory still exists, and dropping
    the claim would leave it unowned and impossible to remove later.
    """
    skipped: tuple[tuple[str, str], ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    problems: tuple[SkillProblem, ...] = ()
    refusal: str = ""
    """Set when the package was refused as a whole and nothing was copied."""

    @property
    def ok(self) -> bool:
        return not self.failed and not self.refusal


def validate_skill(directory: Path) -> tuple[PackageSkill | None, str | None]:
    """Check one skill directory against the Agent Skills rules.

    Returns the skill, or ``None`` and the reason it cannot be used.
    """
    directory = Path(directory)
    skill_md = directory / SKILL_FILENAME
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"the SKILL.md is unreadable: {exc}"

    front, _body = parse_frontmatter(text, line_fallback=True)

    name = front.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "the frontmatter carries no name"
    name = name.strip()
    if len(name) > _MAX_NAME_LENGTH:
        return None, f"the name is {len(name)} characters (max {_MAX_NAME_LENGTH})"
    if not _NAME_RE.match(name):
        return None, f"the name {name!r} must be lowercase letters, digits and single hyphens"
    if name != directory.name:
        return None, f"the name {name!r} does not match the directory name {directory.name!r}"

    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, "the frontmatter carries no description"
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        return None, f"the description is {len(description)} characters (max {_MAX_DESCRIPTION_LENGTH})"

    return PackageSkill(name=name, directory=directory, skill_md=skill_md), None


def discover_skills(
    package: AgentPluginPackage,
) -> tuple[list[PackageSkill], list[SkillProblem]]:
    """The package's usable skills, and the ones that cannot be used.

    A package without a ``skills/`` directory simply has no skills; that is
    not an error (spec §6.2).
    """
    skills_root = package.skills_dir
    if not skills_root.exists():
        return [], []
    if not skills_root.is_dir():
        return [], [SkillProblem(package.name, "the package's skills/ is not a directory")]
    if not is_contained(package.root, skills_root):
        return [], [SkillProblem(package.name, "the package's skills/ resolves outside the package root")]

    skills: list[PackageSkill] = []
    problems: list[SkillProblem] = []
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / SKILL_FILENAME).is_file():
            # Only an immediate child holding a SKILL.md is a skill; the
            # specification forbids searching deeper (spec §7.1).
            continue
        if not is_contained(package.root, child):
            problems.append(SkillProblem(child.name, "the skill directory resolves outside the package root"))
            continue
        # The SKILL.md itself is a path too: a package may keep the skill
        # directory inside its root and point the file at one outside it.
        if not is_contained(package.root, child / SKILL_FILENAME):
            problems.append(SkillProblem(child.name, "the SKILL.md resolves outside the package root"))
            continue
        skill, reason = validate_skill(child)
        if skill is None:
            problems.append(SkillProblem(child.name, reason or "the skill is not usable"))
            continue
        skills.append(skill)
    return skills, problems


def import_package(
    package: AgentPluginPackage,
    *,
    workspace: Path,
    ledger: PackageLedger,
    blocklist: Iterable[str] = (),
) -> ImportReport:
    """Copy a package's skills into the workspace skill tree.

    The copy lands in ``<workspace>/skills/<package name>/<skill name>/``,
    which is what gives every package its own source label in the pool.
    """
    off = normalize_blocklist(blocklist)
    target_root = Path(workspace) / "skills" / package.name
    if package.name in RESERVED_SOURCE_LABELS:
        # The first directory level under the workspace is the pool's source
        # label; a package there would answer as the layer itself.
        return ImportReport(
            package=package.name,
            version=package.version,
            target=str(target_root),
            refusal=(f"the package name {package.name!r} is reserved for a skill layer; choose another name"),
        )
    skills, problems = discover_skills(package)

    if target_root.exists() and not ledger.owns_directory(target_root, workspace=workspace):
        # The package directory would land on top of a directory the ledger
        # does not claim -- the operator's own skill, most likely. Merging a
        # package into it is never what anyone meant.
        return ImportReport(
            package=package.name,
            version=package.version,
            target=str(target_root),
            problems=tuple(problems),
            refusal=(
                f"{target_root} already exists and is not owned by the package ledger; "
                "move or remove that directory, then import again"
            ),
        )

    installed: list[str] = []
    skipped: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    claimed: set[str] = set()

    for skill in skills:
        if skill.name in claimed:
            skipped.append((skill.name, "another skill of this package already claims the name"))
            continue
        claimed.add(skill.name)

        if is_blocked(off, skill.name):
            skipped.append((skill.name, "the name is on the operator blocklist"))
            continue

        try:
            body = skill.skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failed.append((skill.name, f"the SKILL.md is unreadable: {exc}"))
            continue
        flagged = lint_external_paths(body)
        if flagged:
            skipped.append((skill.name, "the body references another product's home directory: " + ", ".join(flagged)))
            continue

        escaped = _escaped_entry(package.root, skill.directory)
        if escaped is not None:
            skipped.append((skill.name, f"{escaped} resolves outside the package root"))
            continue

        target = target_root / skill.name
        if target.exists():
            if not ledger.owns_directory(target, workspace=workspace):
                skipped.append((skill.name, f"{target} already exists and is not owned by the package ledger"))
                continue
            shutil.rmtree(target, ignore_errors=True)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill.directory, target)
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            failed.append((skill.name, str(exc)))
            continue

        try:
            drop_always_flag(target / SKILL_FILENAME)
        except (OSError, ValueError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            failed.append((skill.name, f"the always flag could not be removed: {exc}"))
            continue
        installed.append(skill.name)

    removed, retained, stale_failed = _remove_stale_skills(
        target_root,
        previous=ledger.entry(package.name),
        installed=installed,
        workspace=workspace,
        ledger=ledger,
    )
    failed.extend(stale_failed)

    return ImportReport(
        package=package.name,
        version=package.version,
        target=str(target_root),
        installed=tuple(installed),
        removed=tuple(removed),
        retained=tuple(retained),
        skipped=tuple(skipped),
        failed=tuple(failed),
        problems=tuple(problems),
    )


def _remove_stale_skills(
    target_root: Path,
    *,
    previous: LedgerEntry | None,
    installed: list[str],
    workspace: Path,
    ledger: PackageLedger,
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Delete what an earlier import installed and this one no longer ships.

    The ledger is the record of what this package put in the workspace. A
    refresh that only visits the skills still present in the source leaves a
    removed skill behind, and the ledger entry rewritten to the new set then
    forgets it was ever ours -- so the copy becomes unowned and no later
    ``remove`` can clear it. Converging to the installed set here is what
    makes a re-import mean "the workspace matches the package".

    Returns the removed names, the names still on disk after a failed
    deletion, and the failures. A retained name stays in the ledger's claim:
    reporting it as removed would strand a directory nothing owns.
    """
    if previous is None:
        return [], [], []
    removed: list[str] = []
    retained: list[str] = []
    failed: list[tuple[str, str]] = []
    for name in sorted(set(previous.skills) - set(installed)):
        stale = target_root / name
        if not (stale.is_dir() and ledger.owns_directory(stale, workspace=workspace)):
            continue
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            if stale.exists():
                retained.append(name)
                failed.append((name, f"could not be removed: {exc}"))
            else:
                # The directory is gone: whatever raised, the removal succeeded.
                removed.append(name)
            continue
        if stale.exists():
            retained.append(name)
            failed.append((name, "still present after removal"))
            continue
        removed.append(name)
    return removed, retained, failed


def _escaped_entry(root: Path, directory: Path) -> str | None:
    """The first path under *directory* that resolves outside *root*.

    ``copytree`` follows a symlink or junction it is given, so checking the
    skill directory alone is not enough: a link one level down walks out of
    the package and its contents land in the workspace as if they were the
    package's own. Every entry is checked before anything is copied.
    """
    for parent, directories, files in os.walk(directory, followlinks=True):
        for name in [*directories, *files]:
            candidate = Path(parent) / name
            if not is_contained(root, candidate):
                return str(candidate)
    return None


def split_frontmatter(text: str) -> tuple[str, str, str] | None:
    """Split *text* into its opening delimiter, its frontmatter and the rest.

    The block closes at a ``---`` line only, the rule the registry's
    ``parse_frontmatter`` uses; a ``...`` line belongs to the block. The
    decision and the rewrite must agree on that boundary, or whatever sits
    between the two splits escapes the rewrite.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[0], "".join(lines[1:index]), "".join(lines[index:])
    return None


def strip_always_keys(block: str) -> str:
    """Drop the ``always`` key and the ``always`` entries under ``metadata``.

    The unconditional-injection flag is what the pool reads to put a skill
    into every turn; a third-party package does not get that. Nothing else
    in the frontmatter is touched.
    """
    lines = block.splitlines(keepends=True)
    kept: list[str] = []
    metadata_indent: int | None = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        content = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if not content or content.startswith("#"):
            kept.append(raw)
            index += 1
            continue
        if metadata_indent is not None and indent <= metadata_indent:
            metadata_indent = None
        inside_metadata = metadata_indent is not None and indent > metadata_indent
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "always" and (indent == 0 or inside_metadata):
            index += 1
            while index < len(lines):
                following = lines[index]
                if not following.strip():
                    break
                following_indent = len(following) - len(following.lstrip())
                if following_indent <= indent:
                    break
                index += 1
            continue
        if indent == 0 and key == "metadata" and not value:
            metadata_indent = 0
        kept.append(raw)
        index += 1
    return "".join(kept)


def drop_always_flag(skill_md: Path) -> None:
    """Remove the always-injection flag from an imported skill.

    The copy is Raven's, so the copy is what gets the flag removed. The
    decision uses the registry's own parse instead of a second grammar: the
    line filter alone misses forms the registry reads as always-on (the
    one-line JSON ``metadata`` mapping, for one). Raises ``OSError`` or
    ``ValueError`` when the copy cannot be shown safe; the caller removes it
    rather than leaving an unowned, always-on skill behind.
    """
    raw = Path(skill_md).read_bytes()
    text = raw.decode("utf-8")
    split = split_frontmatter(text)
    if split is None:
        return
    opening, block, rest = split
    frontmatter, _body = parse_frontmatter(opening + block + rest, line_fallback=True, preserve_body_whitespace=True)
    if not isinstance(frontmatter, dict) or not frontmatter:
        raise ValueError("the SKILL.md frontmatter is unreadable")
    nested = nested_metadata(frontmatter.get("metadata", ""))
    if not resolve_always(frontmatter, nested):
        stripped = strip_always_keys(block)
        if stripped != block:
            Path(skill_md).write_bytes((opening + stripped + rest).encode("utf-8"))
            _assert_always_gone(skill_md)
        return
    cleaned = _frontmatter_without_always(frontmatter)
    dumped = yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True)
    Path(skill_md).write_bytes((opening + dumped + rest).encode("utf-8"))
    _assert_always_gone(skill_md)


def _assert_always_gone(skill_md: Path) -> None:
    """Re-read with the registry's parse; a surviving flag is not a success."""
    text = Path(skill_md).read_text(encoding="utf-8")
    frontmatter, _body = parse_frontmatter(text, line_fallback=True, preserve_body_whitespace=True)
    if not isinstance(frontmatter, dict):
        raise ValueError("the rewritten SKILL.md frontmatter is unreadable")
    nested = nested_metadata(frontmatter.get("metadata", ""))
    if resolve_always(frontmatter, nested):
        raise ValueError("the always flag survived the rewrite")


def _frontmatter_without_always(frontmatter: dict) -> dict:
    """The parsed frontmatter with every form of the flag removed.

    ``metadata`` may be a mapping or a JSON string; the string keeps its
    form so the file's shape changes no more than the removal needs.
    """
    data = dict(frontmatter)
    data.pop("always", None)
    raw_meta = data.get("metadata")
    encoded = isinstance(raw_meta, str)
    meta = raw_meta
    if encoded:
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            return data
    if not isinstance(meta, dict):
        return data
    changed = False
    for namespace in ("raven", "nanobot", "openclaw"):
        value = meta.get(namespace)
        if isinstance(value, dict) and "always" in value:
            meta = {**meta, namespace: {key: item for key, item in value.items() if key != "always"}}
            changed = True
    if changed:
        data["metadata"] = json.dumps(meta) if encoded else meta
    return data


__all__ = [
    "SKILL_FILENAME",
    "ImportReport",
    "PackageSkill",
    "SkillProblem",
    "discover_skills",
    "drop_always_flag",
    "import_package",
    "split_frontmatter",
    "strip_always_keys",
    "validate_skill",
]
