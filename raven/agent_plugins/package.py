"""Agent Plugin package discovery.

A package is a directory whose root holds a ``plugin.json`` that validates
against the Agent Plugins 1.0.0 manifest schema (spec §5.1). The package
root is the unit: every path the loader touches has to resolve inside it
(spec §4.1).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from raven.agent_plugins.manifest import (
    MANIFEST_FILENAME,
    AgentPluginManifest,
    AgentPluginManifestError,
    read_manifest,
)


@dataclass(frozen=True)
class PackageProblem:
    """A path that did not yield a usable package."""

    path: str
    reason: str


@dataclass(frozen=True)
class AgentPluginPackage:
    """A validated Agent Plugins package on disk."""

    name: str
    version: str
    root: Path
    manifest: AgentPluginManifest
    notes: tuple[str, ...] = ()

    @property
    def skills_dir(self) -> Path:
        """The fixed skills location (spec §6.1)."""
        return self.root / "skills"

    @property
    def mcp_config_path(self) -> Path:
        """The fixed MCP configuration location (spec §6.1)."""
        return self.root / "mcp.json"

    def fingerprint(self) -> str:
        """A content hash of the package, for drift detection."""
        return package_fingerprint(self.root)


def is_contained(root: Path, candidate: Path) -> bool:
    """True when *candidate* resolves inside *root*.

    The resolution is what makes it safe: a symlink or junction that
    points out of the package is caught here rather than by a path
    comparison on the unresolved name (spec §4.1).
    """
    try:
        candidate.resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def load_package(root: Path) -> AgentPluginPackage:
    """Validate the package rooted at *root*.

    Raises :class:`AgentPluginManifestError` when the package does not
    conform.
    """
    root = Path(root)
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise AgentPluginManifestError(f"the directory holds no {MANIFEST_FILENAME}")
    # The manifest is a package path like any other: a link at the root may
    # point at a file the package does not own (spec §4.1).
    if not is_contained(root, manifest_path):
        raise AgentPluginManifestError(f"{manifest_path} resolves outside the package root")
    manifest, notes = read_manifest(manifest_path)
    return AgentPluginPackage(
        name=manifest.name,
        version=manifest.version or "",
        root=root,
        manifest=manifest,
        notes=notes,
    )


def candidate_roots(entry: Path) -> list[Path]:
    """The package roots an operator-supplied path may point at.

    An entry is either a package root itself or a directory whose
    immediate children are package roots.
    """
    entry = Path(entry)
    if (entry / MANIFEST_FILENAME).is_file():
        return [entry]
    if not entry.is_dir():
        return []
    return sorted(child for child in entry.iterdir() if child.is_dir() and (child / MANIFEST_FILENAME).is_file())


def discover_packages(
    paths: Iterable[str | Path],
    *,
    disabled: Iterable[str] = (),
) -> tuple[list[AgentPluginPackage], list[PackageProblem]]:
    """Find the packages under *paths*.

    Returns the packages that validated and the problems worth reporting.
    A package whose name is in *disabled* is left out.
    """
    off = {str(name) for name in disabled}
    packages: list[AgentPluginPackage] = []
    problems: list[PackageProblem] = []
    seen: dict[str, Path] = {}

    for raw in paths:
        entry = Path(raw).expanduser()
        roots = candidate_roots(entry)
        if not roots:
            problems.append(PackageProblem(str(entry), f"no {MANIFEST_FILENAME} here or in any child directory"))
            continue
        for root in roots:
            try:
                package = load_package(root)
            except AgentPluginManifestError as exc:
                problems.append(PackageProblem(str(root), str(exc)))
                continue
            if package.name in off:
                continue
            previous = seen.get(package.name)
            if previous is not None:
                problems.append(
                    PackageProblem(str(root), f"the package name {package.name!r} is already taken by {previous}")
                )
                continue
            seen[package.name] = root
            packages.append(package)

    packages.sort(key=lambda package: package.name)
    return packages, problems


def package_fingerprint(root: Path) -> str:
    """A content hash over every file in the package."""
    root = Path(root)
    digest = hashlib.sha256()
    try:
        files = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:
        return "sha256:"
    for path in files:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


__all__ = [
    "AgentPluginPackage",
    "PackageProblem",
    "candidate_roots",
    "discover_packages",
    "is_contained",
    "load_package",
    "package_fingerprint",
]
