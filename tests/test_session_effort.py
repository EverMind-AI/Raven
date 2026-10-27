"""raven.agent.session_effort: the pick on a session's record and how it is read."""

from __future__ import annotations

from pathlib import Path

from raven.agent import session_effort
from raven.session.manager import SessionManager


def test_a_record_with_no_pick_reads_as_none() -> None:
    assert session_effort.of({}) is None
    assert session_effort.of({"reasoning_effort": None}) is None
    assert session_effort.of(None) is None
    assert session_effort.of("high") is None


def test_only_a_word_this_raven_knows_reads_as_a_pick() -> None:
    assert session_effort.of({"reasoning_effort": "high"}) == "high"
    assert session_effort.of({"reasoning_effort": "turbo"}) is None
    assert session_effort.of({"reasoning_effort": 3}) is None


def test_a_read_needs_a_manager_and_a_key_and_caches_nothing(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    assert session_effort.read(None, "tui:1") is None
    assert session_effort.read(mgr, "") is None
    assert session_effort.read(mgr, "tui:never") is None
    # peek, not get_or_create: the miss left no ghost behind.
    assert mgr.peek("tui:never") is None


def test_a_write_reaches_the_disk_for_a_filed_session(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    mgr.save(mgr.get_or_create("tui:1"))
    assert session_effort.write(mgr, "tui:1", "low") is True
    assert session_effort.read(mgr, "tui:1") == "low"
    assert session_effort.read(SessionManager(tmp_path), "tui:1") == "low"
    assert session_effort.write(mgr, "tui:1", None) is True
    assert session_effort.read(SessionManager(tmp_path), "tui:1") is None


def test_a_write_on_an_unfiled_session_waits_on_the_copy_in_memory(tmp_path: Path) -> None:
    mgr = SessionManager(tmp_path)
    assert session_effort.write(mgr, "tui:draft", "high") is False
    assert session_effort.read(mgr, "tui:draft") == "high"
    mgr.save(mgr.get_or_create("tui:draft"))
    assert session_effort.read(SessionManager(tmp_path), "tui:draft") == "high"


def test_a_manager_that_cannot_patch_still_holds_the_pick(tmp_path: Path) -> None:
    class _Broken(SessionManager):
        def append_metadata_patch(self, key, patch):
            raise OSError("disk went away")

    mgr = _Broken(tmp_path)
    assert session_effort.write(mgr, "tui:1", "medium") is False
    assert session_effort.read(mgr, "tui:1") == "medium"


def test_in_force_ranks_the_pick_over_the_mode_over_the_configured_default() -> None:
    assert session_effort.in_force("minimal", "medium", "high") == "minimal"
    assert session_effort.in_force(None, "medium", "high") == "medium"
    assert session_effort.in_force(None, None, "high") == "high"


def test_in_force_answers_none_for_the_providers_own_default() -> None:
    """None, not absence: the turn sends it and the RPC reports it as one answer."""
    assert session_effort.in_force(None, None, None) is None
    assert session_effort.in_force("", "", "") is None
