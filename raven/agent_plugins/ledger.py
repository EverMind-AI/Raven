"""The Agent Plugin package ledger.

The record of what was imported into which workspace, from where, at which
version, and what the installed copy looked like. It is what makes removal
safe: only the directories the ledger can prove it installed are ever
deleted.

Version 2 binds every record to the workspace it was imported into and to
the directories themselves. Each install leaves a random marker file at the
package root and at every skill root it creates, and the ledger records the
tokens those markers carry. A directory is owned only while its marker still
matches the recorded token, so a directory the operator put at the same path
-- after moving the install away, or in another workspace sharing the home --
is never mistaken for the imported copy. Records written by version 1 carry
no markers; they load as unverified legacy records that grant nothing.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

LEDGER_VERSION = 2

INSTALL_MARKER_FILENAME = ".raven-install.json"
"""The file whose random token identifies a directory Raven installed."""


class LedgerError(Exception):
    """Raised when the ledger cannot be rewritten safely."""


@dataclass(frozen=True)
class Ownership:
    """Whether the ledger owns a directory; when it does not, why."""

    owned: bool
    reason: str = ""


def workspace_key(workspace: Path) -> str:
    """The ledger's identity of a workspace.

    ``resolve`` makes a linked workspace and its target one key, and drops
    the ``..`` detours and trailing separators different callers spell the
    same directory with; ``normcase`` applies the platform's own case rule
    instead of a blanket lowercase that would merge distinct directories on
    POSIX.
    """
    path = Path(workspace)
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(path))
    return os.path.normcase(str(resolved))


def install_marker_path(directory: Path) -> Path:
    """Where a directory's install marker sits."""
    return Path(directory) / INSTALL_MARKER_FILENAME


def read_install_marker(directory: Path) -> str:
    """The token the install marker in *directory* carries, or "" when none
    can be read."""
    try:
        raw = json.loads(install_marker_path(directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    token = raw.get("token")
    return token if isinstance(token, str) and token else ""


def write_install_marker(directory: Path) -> str:
    """Create a fresh install marker in *directory* and return its token.

    Only the host creates markers, and only in a directory it has just
    created or verified: the token is random per install, so a marker a
    package ships or an operator copies can never match a recorded one.
    """
    token = secrets.token_hex(16)
    restore_install_marker(directory, token)
    return token


def restore_install_marker(directory: Path, token: str) -> None:
    """Put a recorded token's marker back after a removal that did not finish.

    A removal that keeps the ledger record must also keep the marker that
    proves it, or the retained record can never be verified again.
    """
    install_marker_path(directory).write_text(
        json.dumps({"token": token}, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class LedgerEntry:
    """One imported package, scoped to the workspace it lives in."""

    name: str
    version: str = ""
    source: str = ""
    imported_at: str = ""
    skills: tuple[str, ...] = ()
    fingerprint: str = ""
    workspace: str = ""
    """The workspace key this record belongs to; empty for legacy records."""
    install_token: str = ""
    skill_tokens: dict[str, str] = field(default_factory=dict)
    unverified: bool = False
    """True when the record carries no proof of what it owns: a migrated v1
    record, or v2 data too damaged to trust. It grants nothing."""


def _parse_entry(name: str, data: object) -> LedgerEntry:
    """One package record from the file, coerced defensively.

    The mapping key is the only name that always survives; anything
    unreadable keeps it and turns the record unverified rather than granting
    claims from damaged data.
    """
    if not isinstance(data, dict):
        return LedgerEntry(name=name, unverified=True)
    unverified = bool(data.get("unverified"))
    skills = data.get("skills")
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        skills = []
        unverified = True
    tokens = data.get("skill_tokens")
    parsed_tokens: dict[str, str] = {}
    if isinstance(tokens, dict):
        for skill, token in tokens.items():
            if isinstance(skill, str) and isinstance(token, str) and token:
                parsed_tokens[skill] = token
    install_token = data.get("install_token")
    if not isinstance(install_token, str):
        install_token = ""
        unverified = True
    return LedgerEntry(
        name=name,
        version=str(data.get("version") or ""),
        source=str(data.get("source") or ""),
        imported_at=str(data.get("imported_at") or ""),
        skills=tuple(skills),
        fingerprint=str(data.get("fingerprint") or ""),
        install_token=install_token,
        skill_tokens=parsed_tokens,
        unverified=unverified,
    )


def _entry_payload(entry: LedgerEntry) -> dict:
    """The JSON form of one record; the workspace key lives above it."""
    payload: dict[str, object] = {
        "version": entry.version,
        "source": entry.source,
        "imported_at": entry.imported_at,
        "skills": list(entry.skills),
        "fingerprint": entry.fingerprint,
    }
    if entry.unverified:
        payload["unverified"] = True
    else:
        payload["install_token"] = entry.install_token
        payload["skill_tokens"] = {
            name: entry.skill_tokens[name] for name in entry.skills if name in entry.skill_tokens
        }
    return payload


class _LedgerDamageError(Exception):
    """A structural break in the file that must not be answered by rewriting it."""


def _section(raw: dict, name: str) -> dict:
    """A top-level mapping section; absent means empty, present-but-wrong is damage."""
    section = raw.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise _LedgerDamageError(f"the ledger's {name} section is not a JSON object")
    return section


def _load_v1(raw: dict) -> dict[str, LedgerEntry]:
    return {
        str(name): replace(_parse_entry(str(name), data), unverified=True)
        for name, data in _section(raw, "packages").items()
    }


def _load_v2(raw: dict) -> tuple[dict[str, dict[str, LedgerEntry]], dict[str, LedgerEntry]]:
    workspaces: dict[str, dict[str, LedgerEntry]] = {}
    for key, packages in _section(raw, "workspaces").items():
        if not isinstance(packages, dict):
            raise _LedgerDamageError(f"the ledger's record of workspace {key!r} is not a JSON object")
        workspaces[str(key)] = {
            str(name): replace(_parse_entry(str(name), data), workspace=str(key)) for name, data in packages.items()
        }
    legacy: dict[str, LedgerEntry] = {
        str(name): replace(_parse_entry(str(name), data), unverified=True)
        for name, data in _section(raw, "legacy").items()
    }
    return workspaces, legacy


class PackageLedger:
    """The on-disk ledger of the imported packages, per workspace."""

    def __init__(
        self,
        path: Path,
        workspaces: dict[str, dict[str, LedgerEntry]] | None = None,
        legacy: dict[str, LedgerEntry] | None = None,
        unrecognized: str = "",
    ) -> None:
        self.path = Path(path)
        self._workspaces: dict[str, dict[str, LedgerEntry]] = {
            key: dict(entries) for key, entries in (workspaces or {}).items()
        }
        self._legacy: dict[str, LedgerEntry] = dict(legacy or {})
        self._unrecognized: str = unrecognized

    @classmethod
    def load(cls, path: Path) -> "PackageLedger":
        """Read the ledger.

        A file this Raven cannot understand yields an empty ledger that
        refuses to save, so an unknown future version -- or a file whose
        structure is broken -- is never answered by destroying it. Records
        from version 1 become unverified legacy records: their information
        is kept, but nothing on disk can be proven theirs, so they grant no
        delete or overwrite.
        """
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except OSError as exc:
            return cls(path, unrecognized=f"the ledger file cannot be read: {exc}")
        except ValueError as exc:
            return cls(path, unrecognized=f"the ledger file is not valid JSON: {exc}")
        if not isinstance(raw, dict):
            return cls(path, unrecognized="the ledger file is not a JSON object")
        version = raw.get("version")
        try:
            if version == 1:
                return cls(path, legacy=_load_v1(raw))
            if version == 2:
                workspaces, legacy = _load_v2(raw)
                return cls(path, workspaces=workspaces, legacy=legacy)
        except _LedgerDamageError as exc:
            return cls(path, unrecognized=str(exc))
        return cls(
            path,
            unrecognized=(
                f"the ledger file has version {version!r}, which this Raven does not support "
                f"(it writes version {LEDGER_VERSION})"
            ),
        )

    def save(self) -> None:
        """Write the ledger, replacing the file in one step.

        Refuses when the file on disk was not understood: a load that could
        not read the data must not be answered by writing over it.
        """
        if self._unrecognized:
            raise LedgerError(f"refusing to rewrite {self.path}: {self._unrecognized}")
        workspaces: dict[str, dict[str, dict]] = {}
        for key, entries in sorted(self._workspaces.items()):
            packages = {name: _entry_payload(entry) for name, entry in sorted(entries.items())}
            if packages:
                workspaces[key] = packages
        payload = {
            "version": LEDGER_VERSION,
            "workspaces": workspaces,
            "legacy": {name: _entry_payload(entry) for name, entry in sorted(self._legacy.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    @property
    def unrecognized(self) -> str:
        """Why the file on disk cannot be trusted, or "" when it can."""
        return self._unrecognized

    def entries_for(self, workspace: Path) -> dict[str, LedgerEntry]:
        """The recorded packages of one workspace, by name."""
        return dict(self._workspaces.get(workspace_key(Path(workspace)), {}))

    @property
    def legacy_entries(self) -> dict[str, LedgerEntry]:
        """The v1 records, which carry no ownership proof."""
        return dict(self._legacy)

    def entry(self, name: str, *, workspace: Path) -> LedgerEntry | None:
        return self._workspaces.get(workspace_key(Path(workspace)), {}).get(name)

    def legacy_entry(self, name: str) -> LedgerEntry | None:
        return self._legacy.get(name)

    def other_workspaces_with(self, name: str, *, workspace: Path) -> list[str]:
        """The workspace keys holding a record of *name* besides *workspace*."""
        key = workspace_key(Path(workspace))
        return sorted(k for k, packages in self._workspaces.items() if k != key and name in packages)

    def record(self, entry: LedgerEntry) -> None:
        """Record *entry* under its workspace.

        A verified record supersedes a legacy record of the same name in any
        workspace: a version 1 record carries no workspace, and the fresh
        install is the one that can be proven.
        """
        if not entry.workspace:
            raise ValueError("a ledger entry must carry the workspace it belongs to")
        self._workspaces.setdefault(entry.workspace, {})[entry.name] = entry
        self._legacy.pop(entry.name, None)

    def forget(self, name: str, *, workspace: Path) -> LedgerEntry | None:
        """Drop this workspace's record of *name*.

        Other workspaces' records of the same name are untouched, and so are
        legacy records, which only their own re-import can replace.
        """
        return self._workspaces.get(workspace_key(Path(workspace)), {}).pop(name, None)

    def owns_directory(self, directory: Path, *, workspace: Path) -> bool:
        """The boolean form of :meth:`claim`."""
        return self.claim(directory, workspace=workspace).owned

    def claim(self, directory: Path, *, workspace: Path) -> Ownership:
        """Whether the ledger can prove it installed *directory*.

        A path match is not ownership: the record must be this workspace's,
        and the directory must still carry the marker of the recorded
        install. Anything else -- no record, another workspace's or a legacy
        record, a missing or foreign or mismatched marker, a link standing
        at the path -- is not owned, with the reason spelled out. This is a
        check of the persisted record, not a lock: it does not close the
        window in which another process could swap the directory.
        """
        try:
            skills_root = (Path(workspace) / "skills").resolve()
        except (OSError, RuntimeError):
            return Ownership(False, "sits under a workspace path that cannot be resolved")
        try:
            target = Path(directory).resolve()
        except (OSError, RuntimeError):
            return Ownership(False, "cannot be resolved")
        try:
            relative = target.relative_to(skills_root)
        except ValueError:
            return Ownership(False, "is outside the workspace skills tree")
        parts = relative.parts
        if len(parts) == 0:
            return Ownership(False, "is the workspace skills tree itself")
        key = workspace_key(Path(workspace))
        entry = self._workspaces.get(key, {}).get(parts[0])
        if entry is None or entry.unverified:
            return Ownership(False, self._unclaimed_reason(parts[0], key))
        if len(parts) == 1:
            expected = entry.install_token
        elif len(parts) == 2:
            if parts[1] not in entry.skills:
                return Ownership(False, "is not owned by the package ledger")
            expected = entry.skill_tokens.get(parts[1], "")
        else:
            return Ownership(False, "lies deeper than any package the ledger claims")
        if not expected:
            return Ownership(False, "is recorded without a usable install identity")
        path = Path(directory)
        try:
            info = path.lstat()
        except OSError:
            return Ownership(False, "does not exist")
        # A symlink or junction at the install path could point anywhere; the
        # boundary is refused rather than followed, whatever the marker says.
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
            return Ownership(False, "is a symlink or junction, which Raven does not follow")
        if not stat.S_ISDIR(info.st_mode):
            return Ownership(False, "is not a directory")
        found = read_install_marker(path)
        if not found:
            return Ownership(False, "carries no readable Raven install marker")
        if found != expected:
            return Ownership(False, "does not match the recorded install identity")
        return Ownership(True)

    def _unclaimed_reason(self, name: str, key: str) -> str:
        if self._legacy.get(name) is not None:
            return "is claimed only by a legacy ledger record, which carries no install identity"
        if self._workspaces.get(key, {}).get(name) is not None:
            return "is claimed only by a ledger record that carries no install identity"
        if any(name in packages for other, packages in self._workspaces.items() if other != key):
            return "is not owned by the package ledger (this name is imported in another workspace)"
        return "is not owned by the package ledger"


__all__ = [
    "INSTALL_MARKER_FILENAME",
    "LEDGER_VERSION",
    "LedgerEntry",
    "LedgerError",
    "Ownership",
    "PackageLedger",
    "install_marker_path",
    "read_install_marker",
    "restore_install_marker",
    "workspace_key",
    "write_install_marker",
]
