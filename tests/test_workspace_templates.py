"""Product-owned workspace prompts refresh on sync; the user's files do not."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from raven.utils.workspace import sync_workspace_templates

# The 357-byte SOUL.md shipped through 2026-10-03, before the voice rules.
_LEGACY_SOUL = (
    "# Soul\n\n"
    "I am Raven \U0001f426‍⬛, a personal AI assistant.\n\n"
    "## Personality\n\n"
    "- Helpful and friendly\n"
    "- Concise and to the point\n"
    "- Curious and eager to learn\n\n"
    "## Values\n\n"
    "- Accuracy over speed\n"
    "- User privacy and safety\n"
    "- Transparency in actions\n\n"
    "## Communication Style\n\n"
    "- Be clear and direct\n"
    "- Explain reasoning when helpful\n"
    "- Ask clarifying questions when needed\n"
)


def _template(name: str) -> str:
    return (files("raven") / "templates" / name).read_text(encoding="utf-8")


def _soul(root: Path) -> Path:
    return root / "agent_memory" / "profile" / "soul.md"


def test_a_previous_release_soul_is_replaced(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")


def test_a_recorded_previous_write_is_replaced(tmp_path: Path) -> None:
    from raven.config.product_render import write_generated

    soul = _soul(tmp_path)
    write_generated(soul, "# Soul\n\nAn older release's voice.\n", owner="raven")

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")


def test_an_edited_soul_is_kept_and_named(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text("# Soul\n\nMy own voice.\n", encoding="utf-8")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_text(encoding="utf-8") == "# Soul\n\nMy own voice.\n"
    assert any("left" in message and "soul.md" in message for message in messages)


def test_soul_local_is_appended(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    (soul.parent / "soul.local.md").write_text("## Mine\n\n- Say less.\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert text.startswith(_template("SOUL.md").rstrip("\n"))
    assert "## Mine" in text
    assert "- Say less." in text


def test_a_second_sync_changes_nothing(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    before = soul.read_text(encoding="utf-8")

    added = sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == before
    assert added == []


def test_the_refresh_records_a_receipt(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    receipt = soul.with_name(".soul.md.generated.sha256")
    assert receipt.is_file()
    assert "raven " in receipt.read_text(encoding="ascii")


def test_a_legacy_root_copy_is_migrated_and_refreshed_once(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text(_LEGACY_SOUL, encoding="utf-8")

    added = sync_workspace_templates(tmp_path, silent=True)

    soul_entries = [entry for entry in added if "soul.md" in entry]
    assert soul_entries == ["agent_memory/profile/soul.md"]
    assert _soul(tmp_path).read_text(encoding="utf-8") == _template("SOUL.md")


def test_the_user_profile_is_never_refreshed(tmp_path: Path) -> None:
    user = tmp_path / "user_memory" / "profile" / "user.md"
    user.parent.mkdir(parents=True)
    user.write_text("my own profile\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert user.read_text(encoding="utf-8") == "my own profile\n"


def test_agent_instructions_are_never_refreshed(tmp_path: Path) -> None:
    agent = tmp_path / "agent_memory" / "profile" / "agent.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("my own instructions\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert agent.read_text(encoding="utf-8") == "my own instructions\n"
