"""Tests for the ``raven sessions`` CLI subapp."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.cli.session_commands import (
    resolve_session_cross_channel,
    session_app,
)
from raven.session.manager import SessionManager, new_chat_id

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
def patched_workspace(workspace: Path, monkeypatch) -> Path:
    monkeypatch.setattr(
        "raven.cli.session_commands.get_workspace_path",
        lambda: workspace,
    )
    return workspace


@pytest.fixture
def manager(workspace: Path) -> SessionManager:
    return SessionManager(workspace)


@pytest.fixture
def two_sessions(manager: SessionManager, workspace: Path, monkeypatch) -> list[str]:
    monkeypatch.setattr(
        "raven.cli.session_commands.get_workspace_path",
        lambda: workspace,
    )
    chat_id_a = new_chat_id()
    chat_id_b = new_chat_id()
    for cid in (chat_id_a, chat_id_b):
        s = manager.get_or_create(f"cli:{cid}")
        s.add_message("user", "hello")
        manager.save(s)
    return [chat_id_a, chat_id_b]


# ── help ──────────────────────────────────────────────────────────────


def test_session_help_works() -> None:
    r = runner.invoke(session_app, ["--help"])
    assert r.exit_code == 0
    assert "session" in r.stdout.lower()


def test_group_registered_as_sessions_not_session() -> None:
    """The group is mounted as ``raven sessions``; the legacy ``raven
    session`` name is gone (hard rename, no deprecated alias)."""
    ok = runner.invoke(app, ["sessions", "--help"])
    assert ok.exit_code == 0
    assert "Manage conversation sessions" in ok.stdout

    gone = runner.invoke(app, ["session", "--help"])
    assert gone.exit_code != 0


def test_session_create_help() -> None:
    r = runner.invoke(session_app, ["create", "--help"])
    assert r.exit_code == 0
    assert "--title" in r.stdout


def test_session_list_help() -> None:
    r = runner.invoke(session_app, ["list", "--help"])
    assert r.exit_code == 0
    assert "--all" in r.stdout


def test_session_resume_help() -> None:
    r = runner.invoke(session_app, ["resume", "--help"])
    assert r.exit_code == 0


def test_session_delete_help() -> None:
    r = runner.invoke(session_app, ["delete", "--help"])
    assert r.exit_code == 0


# ── create ────────────────────────────────────────────────────────────


def test_create_prints_id(patched_workspace: Path) -> None:
    """First stdout line is exactly the bare chat_id (scripting contract)."""
    import re

    r = runner.invoke(session_app, ["create"])
    assert r.exit_code == 0
    first_line = r.stdout.splitlines()[0].strip()
    assert re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", first_line), f"expected bare chat_id on line 1, got {first_line!r}"


def test_create_lazy_no_file(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["create"])
    assert r.exit_code == 0
    sessions_dir = patched_workspace / "sessions" / "cli"
    assert not sessions_dir.exists() or not list(sessions_dir.glob("*.jsonl")), (
        "bare `session create` must not write a file (lazy)"
    )


def test_create_with_title_persists(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["create", "--title", "My Session"])
    assert r.exit_code == 0
    sessions_dir = patched_workspace / "sessions" / "cli"
    jsonl_files = list(sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []
    assert jsonl_files, "`session create --title` must persist metadata immediately"


def test_create_with_title_output_contains_id(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["create", "--title", "Work"])
    assert r.exit_code == 0
    assert any("_" in line for line in r.stdout.splitlines())


# ── list ──────────────────────────────────────────────────────────────


def test_list_empty_cli_channel(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["list"])
    assert r.exit_code == 0


def test_list_shows_cli_sessions(two_sessions: list[str], patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["list"])
    assert r.exit_code == 0
    for cid in two_sessions:
        assert cid in r.stdout, f"expected bare id {cid!r} in list output"


def test_list_default_cli_only(two_sessions: list[str], patched_workspace: Path, manager: SessionManager) -> None:
    other_cid = new_chat_id()
    s = manager.get_or_create(f"feishu:{other_cid}")
    s.add_message("user", "hi")
    manager.save(s)

    r = runner.invoke(session_app, ["list"])
    assert r.exit_code == 0
    assert other_cid not in r.stdout, "default list must not show feishu sessions"


def test_list_all_includes_other_channels(
    two_sessions: list[str], patched_workspace: Path, manager: SessionManager
) -> None:
    other_cid = new_chat_id()
    s = manager.get_or_create(f"feishu:{other_cid}")
    s.add_message("user", "hi")
    manager.save(s)

    r = runner.invoke(session_app, ["list", "--all"])
    assert r.exit_code == 0
    assert other_cid in r.stdout, "--all must include sessions from other channels"


# ── resume ────────────────────────────────────────────────────────────


def test_resume_full_id(two_sessions: list[str], patched_workspace: Path) -> None:
    cid = two_sessions[0]
    r = runner.invoke(session_app, ["resume", cid])
    assert r.exit_code == 0
    assert cid in r.stdout


def test_resume_prefix_match(two_sessions: list[str], patched_workspace: Path) -> None:
    cid = two_sessions[0]
    prefix = cid[:20]
    r = runner.invoke(session_app, ["resume", prefix])
    assert r.exit_code == 0
    assert cid in r.stdout


def test_resume_not_found(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["resume", "nonexistent000"])
    assert r.exit_code != 0


def test_resume_ambiguous_prefix(patched_workspace: Path, manager: SessionManager) -> None:
    cid_a = "20990101_000000_aaaaaa"
    cid_b = "20990101_000000_bbbbbb"
    for cid in (cid_a, cid_b):
        s = manager.get_or_create(f"cli:{cid}")
        s.add_message("user", "x")
        manager.save(s)

    r = runner.invoke(session_app, ["resume", "20990101"])
    assert r.exit_code != 0
    out = r.stdout.lower()
    assert "ambiguous" in out or "candidates" in out
    assert cid_a in r.stdout
    assert cid_b in r.stdout


def test_resume_exact_match_wins_over_prefix(patched_workspace: Path, manager: SessionManager) -> None:
    """A bare id that equals another id's prefix must exact-match, not
    be reported as ambiguous."""
    short_cid = "20990101_000000_aaa"
    long_cid = "20990101_000000_aaaaaa"
    for cid in (short_cid, long_cid):
        s = manager.get_or_create(f"cli:{cid}")
        s.add_message("user", "x")
        manager.save(s)

    r = runner.invoke(session_app, ["resume", short_cid])
    assert r.exit_code == 0, r.stdout
    first_line = r.stdout.splitlines()[0].strip()
    assert first_line == f"cli:{short_cid}"


# ── resolve_session_cross_channel ─────────────────────────────────────


def _seed(manager: SessionManager, key: str) -> None:
    s = manager.get_or_create(key)
    s.add_message("user", "hi")
    manager.save(s)


def test_cross_channel_full_key_passthrough(manager: SessionManager) -> None:
    """A value already carrying ``:`` is returned verbatim, no lookup."""
    assert resolve_session_cross_channel(manager, "feishu:abc123") == "feishu:abc123"


def test_cross_channel_bare_exact_cli(manager: SessionManager) -> None:
    cid = "20990101_000000_aaaaaa"
    _seed(manager, f"cli:{cid}")
    assert resolve_session_cross_channel(manager, cid) == f"cli:{cid}"


def test_cross_channel_bare_exact_tui(manager: SessionManager) -> None:
    """Cross-channel: a bare id living under tui resolves to its tui key."""
    cid = "20990101_000000_bbbbbb"
    _seed(manager, f"tui:{cid}")
    assert resolve_session_cross_channel(manager, cid) == f"tui:{cid}"


def test_cross_channel_bare_prefix_unique(manager: SessionManager) -> None:
    cid = "20990101_000000_cccccc"
    _seed(manager, f"tui:{cid}")
    assert resolve_session_cross_channel(manager, cid[:20]) == f"tui:{cid}"


def test_cross_channel_ambiguous_same_id_two_channels(
    manager: SessionManager,
) -> None:
    """Same bare id under two channels → ambiguous, must not guess."""
    cid = "20990101_000000_dddddd"
    _seed(manager, f"cli:{cid}")
    _seed(manager, f"tui:{cid}")
    with pytest.raises(typer.BadParameter):
        resolve_session_cross_channel(manager, cid)


def test_cross_channel_ambiguous_prefix_two_matches(
    manager: SessionManager,
) -> None:
    _seed(manager, "cli:20990101_000000_eeeeee")
    _seed(manager, "tui:20990101_000000_ffffff")
    with pytest.raises(typer.BadParameter):
        resolve_session_cross_channel(manager, "20990101")


def test_cross_channel_unknown_bare_falls_back_to_cli(
    manager: SessionManager,
) -> None:
    """No match anywhere → fall back to cli:<value> (never a colon-less key)."""
    assert resolve_session_cross_channel(manager, "nope000") == "cli:nope000"


# ── delete ────────────────────────────────────────────────────────────


def test_delete_by_bare_id(two_sessions: list[str], patched_workspace: Path, manager: SessionManager) -> None:
    cid = two_sessions[0]
    r = runner.invoke(session_app, ["delete", cid])
    assert r.exit_code == 0
    assert not manager.exists(f"cli:{cid}"), "file should be deleted"


def test_delete_by_full_key(two_sessions: list[str], patched_workspace: Path, manager: SessionManager) -> None:
    cid = two_sessions[1]
    r = runner.invoke(session_app, ["delete", f"cli:{cid}"])
    assert r.exit_code == 0
    assert not manager.exists(f"cli:{cid}")


def test_delete_unknown_id_nonzero(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["delete", "00000000_000000_aaaaaa"])
    assert r.exit_code != 0


# ── fork ──────────────────────────────────────────────────────────────


def test_session_fork_help() -> None:
    r = runner.invoke(session_app, ["fork", "--help"])
    assert r.exit_code == 0
    assert "fork" in r.stdout.lower()


def test_fork_prints_child_id(two_sessions: list[str], patched_workspace: Path, manager: SessionManager) -> None:
    cid = two_sessions[0]
    r = runner.invoke(session_app, ["fork", cid])
    assert r.exit_code == 0, r.stdout
    child_bare = r.stdout.strip().splitlines()[0].strip()
    assert child_bare != cid
    assert manager.exists(f"cli:{child_bare}")
    child = manager.get_or_create(f"cli:{child_bare}")
    assert child.metadata["parent_session_id"] == f"cli:{cid}"


def test_fork_prefix_match(two_sessions: list[str], patched_workspace: Path) -> None:
    cid = two_sessions[0]
    r = runner.invoke(session_app, ["fork", cid[:20]])
    assert r.exit_code == 0, r.stdout


def test_fork_unknown_id_errors(patched_workspace: Path) -> None:
    r = runner.invoke(session_app, ["fork", "nonexistent000"])
    assert r.exit_code != 0


def test_fork_empty_session_errors(patched_workspace: Path, manager: SessionManager) -> None:
    cid = new_chat_id()
    empty = manager.get_or_create(f"cli:{cid}")
    manager.save(empty)  # persisted metadata line, zero messages
    assert manager.exists(f"cli:{cid}")

    r = runner.invoke(session_app, ["fork", cid])
    assert r.exit_code != 0
    normalized = " ".join(r.stdout.split())
    assert "session is empty or missing" in normalized


def test_fork_title_option(two_sessions: list[str], patched_workspace: Path, manager: SessionManager) -> None:
    cid = two_sessions[0]
    r = runner.invoke(session_app, ["fork", cid, "--title", "Spinoff"])
    assert r.exit_code == 0, r.stdout
    child_bare = r.stdout.strip().splitlines()[0].strip()
    child = manager.get_or_create(f"cli:{child_bare}")
    assert child.metadata["title"] == "Spinoff"


# ── session export ──────────────────────────────────────────────────────


def test_export_by_bare_id_writes_markdown(patched_workspace: Path, manager: SessionManager) -> None:
    cid = "20990101_000000_abcdef"
    _seed(manager, f"cli:{cid}")
    r = runner.invoke(session_app, ["export", cid])
    assert r.exit_code == 0, r.output
    files = list((patched_workspace / "exports").glob("*.md"))
    assert len(files) == 1
    assert "hi" in files[0].read_text(encoding="utf-8")


def test_export_custom_output_path(patched_workspace: Path, manager: SessionManager, tmp_path: Path) -> None:
    cid = "20990101_000000_bbbbbb"
    _seed(manager, f"cli:{cid}")
    dest = tmp_path / "custom" / "out.md"
    r = runner.invoke(session_app, ["export", cid, "--output", str(dest)])
    assert r.exit_code == 0, r.output
    assert dest.exists()
    assert "hi" in dest.read_text(encoding="utf-8")


def test_export_unknown_id_exits_nonzero_writing_nothing(
    patched_workspace: Path,
) -> None:
    r = runner.invoke(session_app, ["export", "nope000"])
    assert r.exit_code != 0
    assert not (patched_workspace / "exports").exists()


def test_export_write_failure_exits_cleanly(patched_workspace: Path, manager: SessionManager, tmp_path: Path) -> None:
    cid = "20990101_000000_cccccc"
    _seed(manager, f"cli:{cid}")
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    dest = blocker / "out.md"
    r = runner.invoke(session_app, ["export", cid, "--output", str(dest)])
    assert r.exit_code != 0
    assert r.exception is None or isinstance(r.exception, SystemExit)
