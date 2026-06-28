"""Migration shim for legacy feat/auto ATTENTION.md / BEHAVIORS.md
to the L4 ``user_memory/`` layout."""

from __future__ import annotations

from pathlib import Path

from raven.utils.helpers import sync_workspace_templates


def test_legacy_attention_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "ATTENTION.md"
    legacy.write_text("## User overrides\n- old override\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    new = tmp_path / "user_memory" / "attention.md"
    assert new.exists()
    assert "old override" in new.read_text(encoding="utf-8")


def test_legacy_behaviors_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "BEHAVIORS.md"
    legacy.write_text("## 2026-05-28 (Thu)\n\n### evt_x — 09:00–09:30\n",
                      encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    new = tmp_path / "user_memory" / "behaviors.md"
    assert new.exists()
    assert "evt_x" in new.read_text(encoding="utf-8")


def test_legacy_behavior_singular_also_migrated(tmp_path: Path) -> None:
    """feat/auto's actual filename was BEHAVIOR.md (singular); accept both."""
    legacy = tmp_path / "BEHAVIOR.md"
    legacy.write_text("## 2026-05-28 (Thu)\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    new = tmp_path / "user_memory" / "behaviors.md"
    assert new.exists()


def test_existing_target_wins_over_legacy(tmp_path: Path) -> None:
    """User edits to the new path must not be clobbered by legacy file."""
    legacy = tmp_path / "ATTENTION.md"
    legacy.write_text("legacy content\n", encoding="utf-8")
    new = tmp_path / "user_memory" / "attention.md"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("user-edited content\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert "user-edited" in new.read_text(encoding="utf-8")
    assert "legacy" not in new.read_text(encoding="utf-8")


def test_empty_stubs_created_when_no_legacy_source(tmp_path: Path) -> None:
    sync_workspace_templates(tmp_path, silent=True)

    assert (tmp_path / "user_memory" / "attention.md").exists()
    assert (tmp_path / "user_memory" / "behaviors.md").exists()
