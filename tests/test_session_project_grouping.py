"""Sessions group by project on the terminal entrypoints, by channel on the gateway."""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path

import pytest

from raven.session.manager import SessionManager
from raven.utils.paths import project_slug


def _reference_slug(path: str) -> str:
    """Claude Code's own algorithm, transliterated from the shipped binary.

        fXi(e) = e.replace(/[^a-zA-Z0-9]/g, "-")
        hut(e) = fold: t = (t << 5) - t + charCodeAt(i) | 0
        Gw(e)  = fXi(e) if len <= 200 else fXi(e)[:200] + "-" + abs(hut(e)).toString(36)

    Kept here rather than imported so the test fails if our implementation
    drifts, instead of drifting along with it.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "-", path)
    if len(slug) <= 200:
        return slug
    raw = path.encode("utf-16-le")
    value = 0
    for (unit,) in struct.iter_unpack("<H", raw):
        value = (value * 31 + unit) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    value = abs(value)
    digits, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return f"{slug[:200]}-{out or '0'}"


@pytest.mark.parametrize(
    "path",
    [
        "/Evermind/sh_evermind/xuedizhan/Raven",
        "/srv/work/my_app",
        "/root",
        "/",
        "/a//b",  # repeated separators survive: replacement is per character
        "/foo/",  # a trailing separator is kept, not stripped
        "/tmp/a b",
        "/very/" + "x" * 250,  # past the 200-char cap: truncated + hashed
        "/deep/" + "/".join(f"seg{i:02d}" for i in range(40)),
        "/emo/\U0001f600/" + "y" * 220,  # non-BMP: the hash reads UTF-16 units
    ],
)
def test_slug_reproduces_the_reference_implementation(path: str) -> None:
    assert project_slug(path) == _reference_slug(path)


def test_slug_matches_real_claude_code_directories() -> None:
    """The transliteration above could be wrong in the same way twice; these
    are directory names the reference actually produced."""
    assert project_slug("/Evermind/sh_evermind/xuedizhan/Raven") == "-Evermind-sh-evermind-xuedizhan-Raven"
    assert project_slug("/Evermind/sh_evermind/xuedizhan/GMemory") == "-Evermind-sh-evermind-xuedizhan-GMemory"


def test_slug_collides_for_a_separator_and_an_underscore(tmp_path: Path) -> None:
    """Documented, deliberate, and the reason project_dir is recorded: this is
    the reference's behaviour and matching it is the point of D9."""
    assert project_slug("/srv/a_b") == project_slug("/srv/a/b")

    manager = SessionManager(tmp_path, project_slug=project_slug("/srv/a_b"), project_dir=Path("/srv/a_b"))
    other = SessionManager(tmp_path, project_slug=project_slug("/srv/a/b"), project_dir=Path("/srv/a/b"))
    manager.save(manager.get_or_create("cli:one"))
    other.save(other.get_or_create("cli:two"))

    # One bucket, but each session still knows which project it belongs to.
    by_key = {s["key"]: s["metadata"]["project_dir"] for s in manager.list_sessions()}
    assert by_key == {"cli:one": "/srv/a_b", "cli:two": "/srv/a/b"}


def test_project_dir_is_not_rewritten_by_a_later_run_elsewhere(tmp_path: Path) -> None:
    """Reopening a session from another directory must not relabel its origin."""
    first = SessionManager(tmp_path, project_slug="-srv-alpha", project_dir=Path("/srv/alpha"))
    first.save(first.get_or_create("cli:one"))

    second = SessionManager(tmp_path, project_slug="-srv-beta", project_dir=Path("/srv/beta"))
    reopened = second.get_or_create("cli:one")

    assert reopened.metadata["project_dir"] == "/srv/alpha"


def test_slug_never_produces_a_traversing_or_empty_segment() -> None:
    """It is used as a bare directory name, so ``.`` and ``..`` must not survive."""
    for directory in ("/", "/..", "/a/../b", "/./x", "/   "):
        slug = project_slug(directory)
        assert slug and slug not in (".", ".."), directory
        assert "/" not in slug


def test_gateway_groups_by_channel(tmp_path: Path) -> None:
    """No slug: one daemon serves every project, so the channel is the grouping."""
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("web:abc")
    manager.save(session)

    assert (tmp_path / "sessions" / "web" / "abc.jsonl").is_file()


def test_terminal_entrypoints_group_by_launch_directory(tmp_path: Path) -> None:
    """Two runs of ``raven agent`` in different checkouts must not interleave."""
    here = SessionManager(tmp_path, project_slug="-srv-alpha")
    there = SessionManager(tmp_path, project_slug="-srv-beta")
    here.save(here.get_or_create("cli:one"))
    there.save(there.get_or_create("cli:two"))

    assert (tmp_path / "sessions" / "-srv-alpha" / "one.jsonl").is_file()
    assert (tmp_path / "sessions" / "-srv-beta" / "two.jsonl").is_file()


def test_a_session_opened_before_grouping_keeps_its_file(tmp_path: Path) -> None:
    """Resuming by id must continue the real transcript, not shadow it.

    A transcript written under ``sessions/cli/`` (before project grouping, or
    by a run started in another directory) is still the session's only file.
    Minting a fresh empty one beside it would look like the history vanished.
    """
    legacy = SessionManager(tmp_path)
    session = legacy.get_or_create("cli:old")
    session.add_message("user", "remember me")
    legacy.save(session)
    assert (tmp_path / "sessions" / "cli" / "old.jsonl").is_file()

    grouped = SessionManager(tmp_path, project_slug="-srv-alpha")
    reopened = grouped.get_or_create("cli:old")

    assert [m["content"] for m in reopened.messages] == ["remember me"]
    assert not (tmp_path / "sessions" / "-srv-alpha" / "old.jsonl").exists()


def test_listing_filters_on_the_key_not_the_directory(tmp_path: Path) -> None:
    """Under project grouping the directory no longer names the channel, so a
    channel filter that trusted it would return nothing."""
    manager = SessionManager(tmp_path, project_slug="-srv-alpha")
    manager.save(manager.get_or_create("cli:one"))
    manager.save(manager.get_or_create("tui:two"))

    assert [s["key"] for s in manager.list_sessions(channel="cli")] == ["cli:one"]
    assert [s["key"] for s in manager.list_sessions(channel="tui")] == ["tui:two"]
    assert len(manager.list_sessions()) == 2


def test_most_recent_chat_id_scans_every_group(tmp_path: Path) -> None:
    """Cron resolves where to deliver a `tui` reminder by this; with project
    grouping there is no `sessions/tui/` directory to look in."""
    manager = SessionManager(tmp_path, project_slug="-srv-alpha")
    manager.save(manager.get_or_create("tui:one"))

    assert manager.find_most_recent_chat_id("tui") == "one"
    assert manager.find_most_recent_chat_id("qq") is None


def test_session_dir_sits_beside_the_transcript(tmp_path: Path) -> None:
    """The metadata directory is the transcript path minus ``.jsonl``, so the
    pair stays together and the ``*.jsonl`` globs never pick the directory up."""
    manager = SessionManager(tmp_path, project_slug="-srv-alpha")
    manager.save(manager.get_or_create("cli:one"))

    transcript = tmp_path / "sessions" / "-srv-alpha" / "one.jsonl"
    assert manager.session_dir("cli:one") == tmp_path / "sessions" / "-srv-alpha" / "one"
    assert transcript.is_file()
    assert [p.name for p in (tmp_path / "sessions" / "-srv-alpha").glob("*.jsonl")] == ["one.jsonl"]


def test_slug_of_the_current_directory_is_stable(tmp_path: Path) -> None:
    """The entrypoints slug ``Path.cwd()``; a relative spelling of the same
    directory must not produce a second group."""
    project = tmp_path / "proj"
    project.mkdir()
    previous = Path.cwd()
    try:
        os.chdir(project)
        assert project_slug(Path.cwd()) == project_slug(project)
    finally:
        os.chdir(previous)


def test_key_from_path_does_not_invent_a_channel_from_a_slug(tmp_path: Path) -> None:
    """The fallback is only reached for a metadata-less file. Returning the slug
    would look like a channel that no session key can ever match, so a channel
    filter would compare against a path and quietly keep or drop the wrong rows.
    """
    assert SessionManager.key_from_path(Path("sessions/cli/abc.jsonl")) == "cli:abc"
    assert SessionManager.key_from_path(Path("sessions/-srv-alpha/abc.jsonl")) == "unknown:abc"


def test_a_keyless_transcript_under_a_slug_is_not_reported_as_a_channel(tmp_path: Path) -> None:
    """`list_sessions` reaches the fallback only for a file whose metadata line
    carries no `key` -- a file with no metadata line at all is skipped by
    `_scan_file` before this. Either way the derived key must not name the slug,
    or a channel filter compares real channel names against a path.
    """
    group = tmp_path / "sessions" / "-srv-alpha"
    group.mkdir(parents=True)
    (group / "orphan.jsonl").write_text(
        '{"_type": "metadata", "created_at": "2026-08-07T00:00:00"}\n{"role": "user", "content": "hi"}\n',
        encoding="utf-8",
    )

    manager = SessionManager(tmp_path, project_slug="-srv-alpha")

    assert [s["key"] for s in manager.list_sessions()] == ["unknown:orphan"]
    assert manager.list_sessions(channel="cli") == []
    assert manager.list_sessions(channel="-srv-alpha") == []


def test_sentinel_style_scan_never_derives_a_channel_from_a_slug(tmp_path: Path) -> None:
    """The sentinel producers call `key_from_path` on every `rglob` hit before
    reading the file, so for them the fallback is the primary path, not a rare
    one (raven/proactive_engine/sentinel/...). A slug parent there would
    attribute the session to a channel named after a filesystem path.
    """
    for parent in ("-srv-alpha", "cli"):
        (tmp_path / "sessions" / parent).mkdir(parents=True)
        (tmp_path / "sessions" / parent / "s.jsonl").write_text("{}\n", encoding="utf-8")

    derived = {SessionManager.key_from_path(p) for p in (tmp_path / "sessions").rglob("*.jsonl")}

    assert derived == {"unknown:s", "cli:s"}
    assert not any(k.startswith("-") for k in derived)


def test_continue_stays_inside_the_project_it_was_launched_from(tmp_path: Path) -> None:
    """`--continue` must not reopen a conversation started in another checkout.

    Resolving across groups also corrupts what it resumes: the transcript
    fallback in `session_path` would then append this project's turns to a
    file filed under the other one.
    """
    for group, chat_id, updated in (
        ("-srv-alpha", "alpha-1", "2026-08-10T01:00:00"),
        ("-srv-beta", "beta-1", "2026-08-10T23:00:00"),
    ):
        (tmp_path / "sessions" / group).mkdir(parents=True)
        (tmp_path / "sessions" / group / f"{chat_id}.jsonl").write_text(
            json.dumps({"_type": "metadata", "key": f"cli:{chat_id}", "updated_at": updated}) + "\n",
            encoding="utf-8",
        )

    manager = SessionManager(tmp_path, project_slug="-srv-alpha")

    assert manager.find_most_recent_chat_id("cli", this_project_only=True) == "alpha-1"
    # Delivery keeps the wide scan: the gateway forwards to whichever session
    # is live, wherever it was started.
    assert manager.find_most_recent_chat_id("cli") == "beta-1"


def test_project_scoping_is_a_noop_without_a_slug(tmp_path: Path) -> None:
    """On the gateway there is no narrower scan to make -- one daemon serves
    every project -- so the flag must not silently return nothing."""
    (tmp_path / "sessions" / "web").mkdir(parents=True)
    (tmp_path / "sessions" / "web" / "w-1.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "web:w-1", "updated_at": "2026-08-10T01:00:00"}) + "\n",
        encoding="utf-8",
    )

    manager = SessionManager(tmp_path)

    assert manager.find_most_recent_chat_id("web", this_project_only=True) == "w-1"


def test_metadata_dir_never_escapes_the_sessions_tree(tmp_path: Path) -> None:
    """`session_dir` is the one derivation the sub-agent history now trusts, so
    the hardening has to live here rather than downstream.

    `safe_filename` leaves `.` and `..` intact because a `.jsonl` suffix always
    follows it. Taking the transcript's stem instead would point `..` out of the
    group directory, and raise `ValueError` on a lone `.`.
    """
    manager = SessionManager(tmp_path, project_slug="-srv-alpha")
    sessions = (tmp_path / "sessions").resolve()

    for key in ("cli:..", "cli:.", "cli:", "cli:../../etc"):
        resolved = manager.session_dir(key).resolve()
        assert resolved.is_relative_to(sessions) and resolved != sessions


def test_continue_still_reaches_a_pre_grouping_session(tmp_path: Path) -> None:
    """Sessions written before grouping sit in the channel directory and carry
    no project attribution, so narrowing must not strand them.

    `session_path` already lets any project adopt one; refusing to find it
    here would mean every pre-upgrade conversation became unreachable from `-c`
    the moment this MR landed.
    """
    (tmp_path / "sessions" / "cli").mkdir(parents=True)
    (tmp_path / "sessions" / "cli" / "legacy-1.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "cli:legacy-1", "updated_at": "2026-08-10T01:00:00"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "sessions" / "-srv-beta").mkdir(parents=True)
    (tmp_path / "sessions" / "-srv-beta" / "beta-1.jsonl").write_text(
        json.dumps({"_type": "metadata", "key": "cli:beta-1", "updated_at": "2026-08-10T23:00:00"}) + "\n",
        encoding="utf-8",
    )

    manager = SessionManager(tmp_path, project_slug="-srv-alpha")

    # The legacy session is reachable; another project's newer one is not.
    assert manager.find_most_recent_chat_id("cli", this_project_only=True) == "legacy-1"


def test_continue_separates_two_projects_that_share_a_slug(tmp_path: Path) -> None:
    """`/srv/a_b` and `/srv/a/b` slug identically, so scoping by group alone
    would still hand one project the other's session.

    This is the collision the design doc names as invisible until `--continue`
    became project-scoped -- which is why `project_dir` was recorded then.
    """
    slug = project_slug("/srv/a_b")
    assert slug == project_slug("/srv/a/b")

    a = SessionManager(tmp_path, project_slug=slug, project_dir=Path("/srv/a_b"))
    b = SessionManager(tmp_path, project_slug=slug, project_dir=Path("/srv/a/b"))
    a.save(a.get_or_create("cli:from-a"))
    b.save(b.get_or_create("cli:from-b"))

    assert a.find_most_recent_chat_id("cli", this_project_only=True) == "from-a"
    assert b.find_most_recent_chat_id("cli", this_project_only=True) == "from-b"
