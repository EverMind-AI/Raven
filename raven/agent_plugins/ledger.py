"""The Agent Plugin package ledger.

The record of what was imported into the workspace, from where, at which
version, and what the installed copy looked like. It is what makes removal
safe: only the directories the ledger claims are ever deleted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LEDGER_VERSION = 1


@dataclass(frozen=True)
class LedgerEntry:
    """One imported package."""

    name: str
    version: str = ""
    source: str = ""
    imported_at: str = ""
    skills: tuple[str, ...] = ()
    fingerprint: str = ""


class PackageLedger:
    """The on-disk ledger of the imported packages."""

    def __init__(self, path: Path, entries: dict[str, LedgerEntry] | None = None) -> None:
        self.path = Path(path)
        self._entries: dict[str, LedgerEntry] = dict(entries or {})

    @classmethod
    def load(cls, path: Path) -> "PackageLedger":
        """Read the ledger, treating an unreadable file as empty."""
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path)
        if not isinstance(raw, dict):
            return cls(path)
        packages = raw.get("packages")
        if not isinstance(packages, dict):
            return cls(path)
        entries: dict[str, LedgerEntry] = {}
        for name, entry in packages.items():
            if not isinstance(entry, dict):
                continue
            skills = entry.get("skills")
            entries[str(name)] = LedgerEntry(
                name=str(name),
                version=str(entry.get("version") or ""),
                source=str(entry.get("source") or ""),
                imported_at=str(entry.get("imported_at") or ""),
                skills=tuple(str(item) for item in skills) if isinstance(skills, list) else (),
                fingerprint=str(entry.get("fingerprint") or ""),
            )
        return cls(path, entries)

    def save(self) -> None:
        """Write the ledger, replacing the file in one step."""
        payload = {
            "version": LEDGER_VERSION,
            "packages": {
                name: {
                    "version": entry.version,
                    "source": entry.source,
                    "imported_at": entry.imported_at,
                    "skills": list(entry.skills),
                    "fingerprint": entry.fingerprint,
                }
                for name, entry in sorted(self._entries.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    @property
    def entries(self) -> dict[str, LedgerEntry]:
        """The recorded packages, by name."""
        return dict(self._entries)

    def entry(self, name: str) -> LedgerEntry | None:
        return self._entries.get(name)

    def record(self, entry: LedgerEntry) -> None:
        self._entries[entry.name] = entry

    def forget(self, name: str) -> LedgerEntry | None:
        return self._entries.pop(name, None)

    def owns_directory(self, directory: Path, *, workspace: Path) -> bool:
        """True when *directory* belongs to a recorded package.

        The package directory (``<workspace>/skills/<package>``) is owned,
        and so are the skill directories the ledger recorded inside it. A
        directory the operator put there by hand is not owned, and an import
        leaves it alone.
        """
        skills_root = (Path(workspace) / "skills").resolve()
        try:
            target = Path(directory).resolve()
            relative = target.relative_to(skills_root)
        except (ValueError, OSError):
            return False
        parts = relative.parts
        if len(parts) == 1:
            return parts[0] in self._entries
        if len(parts) == 2:
            entry = self._entries.get(parts[0])
            return entry is not None and parts[1] in entry.skills
        return False


__all__ = ["LEDGER_VERSION", "LedgerEntry", "PackageLedger"]
