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
    legacy.write_text("## 2026-05-28 (Thu)\n\n### evt_x — 09:00–09:30\n", encoding="utf-8")

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


# The guarantee bundled before the fact gate was removed. Wrapped exactly as the
# template shipped it, because an existing workspace's agent.md holds those bytes
# verbatim and the migration retires that exact span.
_STALE_AGENTS_GUARANTEE = (
    "The build refuses a deck with a number no source printed, a page citing one figure\n"
    "while showing another, a length the brief did not agree, the wrong language, or a\n"
    "theme that is not the bound template's."
)


def test_stale_bundled_guarantee_in_an_existing_workspace_is_retired(tmp_path: Path) -> None:
    """Step 2 fills only missing files, so an agent.md seeded by the previous bundled
    template keeps a guarantee the code no longer delivers. The sync retires exactly
    that sentence, leaves the user's own notes alone, and the prompt stops carrying it.

    Rendered with the real bootstrap path rather than read from the file alone, because
    that is what a resumed job's author model sees."""
    from raven.context_engine.segments.render import load_bootstrap_files

    profile = tmp_path / "agent_memory" / "profile" / "agent.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "# What this agent may do\n\n" + _STALE_AGENTS_GUARANTEE + "\n\n## My own note\n\nkeep exactly this\n",
        encoding="utf-8",
    )

    sync_workspace_templates(tmp_path, silent=True)

    text = profile.read_text(encoding="utf-8")
    assert "a number no source printed" not in text, "the stale guarantee survived the sync"
    assert "It does not check whether a number appears in a source" in text
    assert "keep exactly this" in text, "the user's own note was clobbered"

    prompt = load_bootstrap_files(tmp_path)
    assert "a number no source printed" not in prompt
    assert "It does not check whether a number appears in a source" in prompt
