"""The Agent Plugin package ledger: what was imported, and what it owns.

The ledger is what makes removal safe -- only the directories the ledger
claims may ever be deleted -- so its load, its round-trip and its ownership
test are the three things that decide whether an uninstall touches anything
it should not.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent_plugins.ledger import LEDGER_VERSION, LedgerEntry, PackageLedger

PACKAGE_NAME = "demo-pack"


def _entry(name: str = PACKAGE_NAME, **overrides: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "name": name,
        "version": "1.0.0",
        "source": "/tmp/demo-pack",
        "imported_at": "2026-10-25T00:00:00Z",
        "skills": ("alpha",),
        "fingerprint": "sha256:0123456789",
    }
    fields.update(overrides)
    return LedgerEntry(**fields)  # type: ignore[arg-type]


def _ledger_with(tmp_path: Path, *names: str) -> PackageLedger:
    ledger = PackageLedger(tmp_path / "ledger.json")
    for name in names:
        ledger.record(_entry(name))
    return ledger


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_on_a_missing_file_gives_an_empty_ledger(tmp_path: Path) -> None:
    ledger = PackageLedger.load(tmp_path / "ledger.json")

    assert ledger.entries == {}
    assert ledger.entry(PACKAGE_NAME) is None


def test_load_on_a_corrupt_file_gives_an_empty_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json at all", encoding="utf-8")

    ledger = PackageLedger.load(path)

    assert ledger.entries == {}
    assert ledger.entry(PACKAGE_NAME) is None


def test_load_ignores_a_file_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text('["demo-pack"]', encoding="utf-8")

    ledger = PackageLedger.load(path)

    assert ledger.entries == {}


# ---------------------------------------------------------------------------
# Recording and saving
# ---------------------------------------------------------------------------


def test_record_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    ledger.record(_entry(PACKAGE_NAME))
    ledger.record(_entry("other-pack", version="2.0.0", skills=("beta", "gamma")))

    ledger.save()

    reloaded = PackageLedger.load(path)
    assert set(reloaded.entries) == {PACKAGE_NAME, "other-pack"}
    assert reloaded.entry(PACKAGE_NAME).version == "1.0.0"
    assert reloaded.entry(PACKAGE_NAME).skills == ("alpha",)
    assert reloaded.entry(PACKAGE_NAME).fingerprint == "sha256:0123456789"
    assert reloaded.entry("other-pack").version == "2.0.0"
    assert reloaded.entry("other-pack").skills == ("beta", "gamma")


def test_the_saved_file_carries_the_version_and_the_packages(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    ledger.record(_entry(PACKAGE_NAME))

    ledger.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == LEDGER_VERSION
    assert payload["version"] == 1
    assert isinstance(payload["packages"], dict)
    assert PACKAGE_NAME in payload["packages"]


def test_forget_removes_the_entry_and_the_removal_survives_a_save(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    ledger.record(_entry(PACKAGE_NAME))
    ledger.record(_entry("other-pack"))
    ledger.save()

    removed = ledger.forget(PACKAGE_NAME)
    assert removed is not None
    assert removed.name == PACKAGE_NAME
    assert PACKAGE_NAME not in ledger.entries

    ledger.save()

    reloaded = PackageLedger.load(path)
    assert PACKAGE_NAME not in reloaded.entries
    assert "other-pack" in reloaded.entries


def test_save_writes_the_file_when_the_parent_directory_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nested" / "ledger.json"
    ledger = PackageLedger(path)
    ledger.record(_entry(PACKAGE_NAME))

    ledger.save()

    assert path.is_file()
    assert PackageLedger.load(path).entry(PACKAGE_NAME) is not None


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_owns_the_recorded_package_directory_and_its_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = _ledger_with(tmp_path, PACKAGE_NAME)

    assert ledger.owns_directory(workspace / "skills" / PACKAGE_NAME, workspace=workspace)
    assert ledger.owns_directory(workspace / "skills" / PACKAGE_NAME / "alpha", workspace=workspace)


def test_does_not_own_a_package_that_is_not_recorded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = _ledger_with(tmp_path, PACKAGE_NAME)

    assert not ledger.owns_directory(workspace / "skills" / "other-pack", workspace=workspace)
    assert not ledger.owns_directory(workspace / "skills" / "other-pack" / "alpha", workspace=workspace)


def test_does_not_own_a_path_outside_the_skills_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = _ledger_with(tmp_path, PACKAGE_NAME)

    assert not ledger.owns_directory(workspace / "elsewhere" / PACKAGE_NAME, workspace=workspace)
    assert not ledger.owns_directory(tmp_path / "outside", workspace=workspace)


def test_does_not_own_a_deeper_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = _ledger_with(tmp_path, PACKAGE_NAME)

    assert not ledger.owns_directory(workspace / "skills" / PACKAGE_NAME / "alpha" / "scripts", workspace=workspace)
