"""Product-owned workspace prompts merge on sync; the user's files do not."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from raven.utils.workspace import _merge_soul, sync_workspace_templates

# The 357-byte SOUL.md shipped through 2026-10-03, before the voice rules.
_LEGACY_SOUL = (
    "# Soul\n\n"
    "I am Raven \U0001F426‍⬛, a personal AI assistant.\n\n"
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


def _base(root: Path) -> Path:
    return _soul(root).with_name(".soul.md.template")


def test_merge_applies_a_product_change_elsewhere() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n\n## B\nbeta\n",
        "## A\nalpha\n\n## B\nbeta (mine)\n",
        "## A\nalpha new\n\n## B\nbeta\n",
    )

    assert merged == "## A\nalpha new\n\n## B\nbeta (mine)\n"
    assert conflicts == []


def test_merge_keeps_the_user_line_on_a_same_line_change() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n",
        "## A\nalpha mine\n",
        "## A\nalpha new\n",
    )

    assert merged == "## A\nalpha mine\n"
    assert conflicts == ["## A"]


def test_merge_keeps_a_user_append_and_a_product_append() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n",
        "## A\nalpha\n\n## Mine\nmine\n",
        "## A\nalpha\n\n## New\nnew\n",
    )

    assert merged == "## A\nalpha\n\n## New\nnew\n\n## Mine\nmine\n"
    assert conflicts == []


def test_merge_takes_a_product_deletion_the_user_did_not_touch() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n\n## B\nbeta\n",
        "## A\nalpha\n\n## B\nbeta\n",
        "## A\nalpha\n",
    )

    assert merged == "## A\nalpha\n"
    assert conflicts == []


def test_merge_keeps_a_user_section_the_product_deleted() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n\n## B\nbeta\n",
        "## A\nalpha\n\n## B\nbeta mine\n",
        "## A\nalpha\n",
    )

    assert "beta mine" in merged
    assert conflicts == ["## B"]


def test_merge_normalises_crlf() -> None:
    merged, conflicts = _merge_soul(
        "## A\nalpha\n",
        "## A\nalpha mine\r\n",
        "## A\nalpha new\n",
    )

    assert merged == "## A\nalpha mine\n"
    assert conflicts == ["## A"]


def test_a_previous_release_soul_is_replaced(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")
    assert _base(tmp_path).read_text(encoding="utf-8") == _template("SOUL.md")


def test_an_old_copy_keeps_the_users_edit_while_the_rest_updates(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    edited = _LEGACY_SOUL.replace("- Concise and to the point", "- Concise and to the point, but mine")
    soul.write_text(edited, encoding="utf-8")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    text = soul.read_text(encoding="utf-8")
    assert "- Concise and to the point, but mine" in text
    assert "## How I talk" in text
    assert "## My stance" in text
    assert any("not updated" in message for message in messages)


def test_a_base_file_merges_a_later_release(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_template("SOUL.md") + "\n## Mine\n\n- Say less.\n", encoding="utf-8")
    _base(tmp_path).write_text(_template("SOUL.md"), encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert text.startswith(_template("SOUL.md").rstrip("\n"))
    assert "## Mine" in text
    assert "- Say less." in text


def test_an_unrecognised_soul_is_kept_and_named(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text("# Soul\n\nMy own voice.\n", encoding="utf-8")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_text(encoding="utf-8") == "# Soul\n\nMy own voice.\n"
    assert any("left" in message and "soul.md" in message for message in messages)


def test_soul_local_is_applied_once(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    (soul.parent / "soul.local.md").write_text("## Mine\n\n- Say less.\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)
    first = soul.read_text(encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)

    assert first.count("## Mine") == 1
    assert soul.read_text(encoding="utf-8") == first


def test_a_non_utf8_local_file_does_not_stop_the_sync(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    (soul.parent / "soul.local.md").write_bytes(b"## Mine\n\n\xff\n")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")
    assert any("UTF-8" in message for message in messages)


def test_a_second_sync_changes_nothing(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    before = soul.read_text(encoding="utf-8")

    added = sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == before
    assert added == []


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


def test_a_legacy_root_copy_is_migrated_and_refreshed_once(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text(_LEGACY_SOUL, encoding="utf-8")

    added = sync_workspace_templates(tmp_path, silent=True)

    soul_entries = [entry for entry in added if "soul.md" in entry]
    assert soul_entries == ["agent_memory/profile/soul.md"]
    assert _soul(tmp_path).read_text(encoding="utf-8") == _template("SOUL.md")


def test_a_crlf_copy_is_recognised_as_the_previous_release(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_bytes(_LEGACY_SOUL.replace("\n", "\r\n").encode("utf-8"))

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")


def test_the_merged_voice_reaches_the_bootstrap_segment(tmp_path: Path) -> None:
    from raven.context_engine.segments import render

    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = render.load_bootstrap_files(tmp_path)
    assert "## How I talk" in text
    assert "corporate filler" in text
