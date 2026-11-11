"""Product-owned workspace prompts merge on sync; the user's files do not."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from raven.utils.workspace import _digest, _merge_soul, sync_workspace_templates

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


def test_merge_applies_a_product_change_beside_a_user_edit_in_one_section() -> None:
    merged, conflicts = _merge_soul(
        "## A\n- one\n- two\n- three\n",
        "## A\n- one\n- two mine\n- three\n",
        "## A\n- one new\n- two\n- three\n",
    )

    assert merged == "## A\n- one new\n- two mine\n- three\n"
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


def test_the_values_heading_and_body_stay_under_a_values_edit() -> None:
    ours = _LEGACY_SOUL.replace("- Accuracy over speed", "- Accuracy over speed, as I see it")

    merged, conflicts = _merge_soul(_LEGACY_SOUL, ours, _template("SOUL.md"))

    assert "## My stance" not in merged
    assert "## Values" in merged
    assert "- Accuracy over speed, as I see it" in merged
    assert "## Communication Style" in merged
    assert conflicts == ["## Values", "## Communication Style"]


def test_a_communication_style_edit_does_not_move_values_under_my_stance() -> None:
    ours = _LEGACY_SOUL.replace("- Ask clarifying questions when needed", "- Ask when the answer changes what I do")

    merged, conflicts = _merge_soul(_LEGACY_SOUL, ours, _template("SOUL.md"))

    assert "## My stance" not in merged
    assert "## Values\n\n- Accuracy over speed\n- User privacy and safety\n" in merged
    assert "- Ask when the answer changes what I do" in merged
    assert conflicts == ["## Values", "## Communication Style"]


def test_a_product_values_rename_lands_when_values_is_not_edited() -> None:
    ours = _LEGACY_SOUL.replace("- Helpful and friendly", "- Helpful and friendly, mine")

    merged, conflicts = _merge_soul(_LEGACY_SOUL, ours, _template("SOUL.md"))

    assert "## My stance" in merged
    assert "I am a guest in someone's work." in merged
    assert "## Values" not in merged
    assert "## Personality" in merged
    assert conflicts == ["## Personality"]


def test_a_conflict_in_one_section_keeps_the_other_sections_changes() -> None:
    base = "## A\n- a one\n- a two\n\n## B\n- b one\n- b two\n\n## C\n- c one\n- c two\n"
    ours = "## A\n- a one\n- a two\n\n## B\n- b one mine\n- b two\n\n## C\n- c one\n- c two\n"
    theirs = "## A new\n- a one new\n- a two\n\n## B\n- b one new\n- b two\n\n## C\n- c one new\n- c two\n"

    merged, conflicts = _merge_soul(base, ours, theirs)

    assert "## A new" in merged
    assert "- a one new" in merged
    assert "- b one mine" in merged
    assert "- b one new" not in merged
    assert "- c one new" in merged
    assert conflicts == ["## B"]


def test_duplicate_headings_do_not_share_one_conflict() -> None:
    base = "## Rules\n- a\n- b\n\n## Rules\n- c\n- d\n"
    ours = "## Rules\n- a mine\n- b\n\n## Rules\n- c\n- d\n"
    theirs = "## Rules\n- a new\n- b\n\n## Rules\n- c new\n- d\n"

    merged, conflicts = _merge_soul(base, ours, theirs)

    assert "- a mine" in merged
    assert "- c new" in merged
    assert conflicts == ["## Rules"]


def test_a_fenced_hash_is_not_a_section_boundary() -> None:
    base = "## A\n```sh\n# check\n```\n- a\n\n## B\n```sh\n# check\n```\n- b\n"
    ours = "## A\n```sh\n# check\n```\n- a mine\n\n## B\n```sh\n# check\n```\n- b\n"
    theirs = "## A\n```sh\n# check\n```\n- a new\n\n## B\n```sh\n# check\n```\n- b new\n"

    merged, conflicts = _merge_soul(base, ours, theirs)

    assert "- b new" in merged
    assert conflicts == ["## A"]


def test_a_product_append_lands_when_the_user_edited_elsewhere() -> None:
    base = "## A\nalpha\n\n## B\nbeta\n"
    ours = "## A\nalpha mine\n\n## B\nbeta\n"
    theirs = base + "\n## New\nnew\n"

    merged, conflicts = _merge_soul(base, ours, theirs)

    assert merged == "## A\nalpha mine\n\n## B\nbeta\n\n## New\nnew\n"
    assert conflicts == []


def test_a_product_append_does_not_glue_to_a_user_line_without_a_newline() -> None:
    merged, conflicts = _merge_soul("## A\nalpha\n", "## A\nalpha mine", "## A\nalpha\n\n## New\nnew\n")

    assert merged == "## A\nalpha mine\n\n## New\nnew\n"
    assert conflicts == []


def test_both_sides_making_the_same_edit_is_not_a_conflict() -> None:
    merged, conflicts = _merge_soul(
        "## A\n- a\n- b\n",
        "## A\n- a\n- b mine\n",
        "## A\n- a\n- b mine\n",
    )

    assert merged == "## A\n- a\n- b mine\n"
    assert conflicts == []


def test_a_previous_release_soul_is_replaced(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")
    assert _base(tmp_path).read_text(encoding="utf-8") == _template("SOUL.md")


def test_an_edited_values_section_is_left_whole_by_the_sync(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    edited = _LEGACY_SOUL.replace("- Accuracy over speed", "- Accuracy over speed, as I see it")
    soul.write_text(edited, encoding="utf-8")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    text = soul.read_text(encoding="utf-8")
    assert "## My stance" not in text
    assert "## Values" in text
    assert "- Accuracy over speed, as I see it" in text
    assert any("not updated:" in message and "## Values" in message for message in messages)
    first = text
    sync_workspace_templates(tmp_path, silent=True)
    assert soul.read_text(encoding="utf-8") == first


def test_an_old_copy_keeps_the_users_edit_while_the_rest_updates(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    edited = _LEGACY_SOUL.replace("- Concise and to the point", "- Concise and to the point, but mine")
    soul.write_text(edited, encoding="utf-8")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    text = soul.read_text(encoding="utf-8")
    assert "- Concise and to the point, but mine" in text
    assert "## Personality" in text
    assert "## How I talk" not in text
    assert "## My stance" in text
    assert any("not updated: ## Personality" in message for message in messages)
    backups = sorted(soul.parent.glob("soul.md.bak-*"))
    assert backups
    assert "- Concise and to the point, but mine" in backups[-1].read_text(encoding="utf-8")


def test_a_base_file_equal_to_the_template_is_not_merged_again(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_template("SOUL.md") + "\n## Mine\n\n- Say less.\n", encoding="utf-8")
    _base(tmp_path).write_text(_template("SOUL.md"), encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert text.startswith(_template("SOUL.md").rstrip("\n"))
    assert "## Mine" in text
    assert "- Say less." in text


def test_a_base_record_from_an_older_release_merges_forward(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL + "\n## Mine\n\n- Say less.\n", encoding="utf-8")
    _base(tmp_path).write_text(_LEGACY_SOUL, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert text.startswith(_template("SOUL.md").rstrip("\n"))
    assert "## How I talk" in text
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


def test_a_product_generated_soul_with_a_receipt_is_left_alone_silently(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    generated = _LEGACY_SOUL + "\n## Product note\n\n- generated\n"
    soul.write_text(generated, encoding="utf-8")
    soul.with_name(".soul.md.generated.sha256").write_text(f"raven-code {_digest(generated)}\n", encoding="ascii")
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_text(encoding="utf-8") == generated
    assert not _base(tmp_path).exists()
    assert not any("nothing shows" in message for message in messages)


def test_a_receipt_naming_a_shipped_text_is_still_a_merge_base(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    soul.with_name(".soul.md.generated.sha256").write_text(f"raven {_digest(_LEGACY_SOUL)}\n", encoding="ascii")

    sync_workspace_templates(tmp_path, silent=True)

    assert soul.read_text(encoding="utf-8") == _template("SOUL.md")


def test_the_shipped_soul_history_merges_cleanly() -> None:
    current = _template("SOUL.md")
    legacy = sorted(
        (entry for entry in (files("raven") / "templates" / "legacy").iterdir() if entry.name.endswith(".md")),
        key=lambda entry: entry.name,
    )

    assert legacy
    assert _digest(current) not in {_digest(entry.read_text(encoding="utf-8")) for entry in legacy}
    for entry in legacy:
        text = entry.read_text(encoding="utf-8")
        merged, conflicts = _merge_soul(text, text, current)
        assert merged == current
        assert conflicts == []


def test_an_addendum_already_in_the_file_is_not_duplicated(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    block = "## Mine\n\n- old rule\n"
    soul.write_text(_template("SOUL.md") + "\n" + block + "\n## After\n\n- more\n", encoding="utf-8")
    (soul.parent / "soul.local.md").write_text(block, encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert text.count("## Mine") == 1
    assert "## After" in text


def test_a_changed_soul_local_replaces_the_old_block(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    local = soul.parent / "soul.local.md"
    local.write_text("## Mine\n\n- old rule\n", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    local.write_text("## Mine\n\n- new rule\n", encoding="utf-8")

    sync_workspace_templates(tmp_path, silent=True)

    text = soul.read_text(encoding="utf-8")
    assert "old rule" not in text
    assert "- new rule" in text
    assert text.count("## Mine") == 1


def test_a_removed_soul_local_withdraws_the_block(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    local = soul.parent / "soul.local.md"
    local.write_text("## Mine\n\n- old rule\n", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    local.unlink()

    sync_workspace_templates(tmp_path, silent=True)

    assert "old rule" not in soul.read_text(encoding="utf-8")


def test_the_applied_addendum_is_recorded(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    local = soul.parent / "soul.local.md"
    record = soul.with_name(".soul.md.addendum")
    local.write_text("## Mine\n\n- old rule\n", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    assert "- old rule" in record.read_text(encoding="utf-8")
    local.write_text("## Mine\n\n- new rule\n", encoding="utf-8")
    sync_workspace_templates(tmp_path, silent=True)
    assert "- new rule" in record.read_text(encoding="utf-8")
    local.unlink()
    sync_workspace_templates(tmp_path, silent=True)
    assert not record.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_the_addendum_record_keeps_the_source_mode(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    local = soul.parent / "soul.local.md"
    local.write_text("## Mine\n\n- private rule\n", encoding="utf-8")
    os.chmod(local, 0o600)

    sync_workspace_templates(tmp_path, silent=True)

    record = soul.with_name(".soul.md.addendum")
    assert stat.S_IMODE(record.stat().st_mode) & 0o077 == 0


def test_the_addendum_record_is_created_from_its_source_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Windows mode cannot carry the source's DACL, so the record write must
    name soul.local.md itself, not only its mode."""
    import raven.utils.workspace as workspace_module

    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    local = soul.parent / "soul.local.md"
    local.write_text("## Mine\n\n- private rule\n", encoding="utf-8")
    writes: list[dict[str, object]] = []
    replace = workspace_module.atomic_replace

    def record(path, *args, **kwargs):
        if Path(path).name == ".soul.md.addendum":
            writes.append(dict(kwargs))
        return replace(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "atomic_replace", record)
    sync_workspace_templates(tmp_path, silent=True)

    assert [(entry["like"], entry["mode"]) for entry in writes] == [(local, stat.S_IMODE(local.stat().st_mode))]


def _windows_dacl(path: Path, scratch: Path) -> tuple[str, set[str]]:
    """The DACL's flags and its set of ACE strings, as ``icacls /save`` writes them."""
    saved = scratch / "dacl.txt"
    saved.unlink(missing_ok=True)
    result = subprocess.run(["icacls", str(path), "/save", str(saved)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    raw = saved.read_bytes()
    text = raw.decode("utf-16-le" if b"\x00" in raw[:4] else "utf-8")
    dacl = re.search(r"D:([A-Z]*)((?:\([^)]*\))+)", text)
    assert dacl, text
    return dacl.group(1), set(re.findall(r"\(([^)]*)\)", dacl.group(2)))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_the_addendum_record_keeps_the_private_sources_dacl(tmp_path: Path) -> None:
    """A new Windows file inherits its folder's ACL, and os.replace carries the
    temp's onto the record: the operator's private text must not come back
    readable by every account the folder lets in."""
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    folder = soul.parent
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"], capture_output=True, check=True)
    local = folder / "soul.local.md"
    local.write_text("## Mine\n\n- private rule\n", encoding="utf-8")
    user = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
    sid = user.stdout.strip().split(",")[-1].strip('"')
    subprocess.run(["icacls", str(local), "/inheritance:r", "/grant:r", f"*{sid}:(F)"], capture_output=True, check=True)
    flags, aces = _windows_dacl(local, tmp_path)
    assert "P" in flags and not any(ace.endswith(";BU") for ace in aces), "the setup made the prompt private"

    sync_workspace_templates(tmp_path, silent=True)

    record = soul.with_name(".soul.md.addendum")
    assert record.read_text(encoding="utf-8") == "## Mine\n\n- private rule\n"
    record_flags, record_aces = _windows_dacl(record, tmp_path)
    assert record_aces == aces
    assert "P" in record_flags


def test_a_refused_soul_write_does_not_stop_the_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import raven.utils.workspace as workspace_module

    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    soul.write_text(_LEGACY_SOUL, encoding="utf-8")
    replace = workspace_module.atomic_replace

    def refuse(path, *args, **kwargs):
        if Path(path).name == "soul.md":
            raise PermissionError("WinError 5")
        return replace(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "atomic_replace", refuse)
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_text(encoding="utf-8") == _LEGACY_SOUL
    assert any("could not refresh" in message for message in messages)


def test_a_non_utf8_soul_is_left_alone(tmp_path: Path) -> None:
    soul = _soul(tmp_path)
    soul.parent.mkdir(parents=True)
    raw = _LEGACY_SOUL.replace("\U0001f426‍⬛", "").encode("gbk") + b"\n- \xd6\xd0\xce\xc4 rule\n"
    soul.write_bytes(raw)
    messages: list[str] = []

    sync_workspace_templates(tmp_path, notify=messages.append)

    assert soul.read_bytes() == raw
    assert any("not UTF-8" in message for message in messages)


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

    soul_entries = [Path(entry).as_posix() for entry in added if "soul.md" in entry]
    assert soul_entries == [Path("agent_memory", "profile", "soul.md").as_posix()]
    assert _soul(tmp_path).read_text(encoding="utf-8") == _template("SOUL.md")


def test_a_crlf_legacy_copy_migrates_without_doubled_newlines(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_bytes(b"# Soul\r\n\r\nMy own voice.\r\n")

    sync_workspace_templates(tmp_path, silent=True)

    raw = _soul(tmp_path).read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.startswith(b"# Soul\r\n\r\nMy own voice.")


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
