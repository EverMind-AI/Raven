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


def test_the_two_rungs_above_high_read_back_as_picks() -> None:
    """A record carries any rung this raven knows, not only the four every
    wire takes: ``max`` picked on a Claude has to come back after a restart,
    and whether the session's wire still takes it is ``fit``'s question."""
    assert session_effort.of({"reasoning_effort": "xhigh"}) == "xhigh"
    assert session_effort.of({"reasoning_effort": "max"}) == "max"


def test_a_pick_is_fitted_to_the_wire_that_runs_it() -> None:
    """``max`` picked on a Claude, then the session moved to a model whose wire
    takes the four: the turn runs at ``high`` -- as much thinking as that wire
    takes -- not at a request it would refuse and not at the default."""
    four = ("minimal", "low", "medium", "high")
    six = four + ("xhigh", "max")
    assert session_effort.fit("max", four) == "high"
    assert session_effort.fit("xhigh", four) == "high"
    assert session_effort.fit("high", four) == "high", "a word the wire takes stands"
    assert session_effort.fit("max", six) == "max"
    assert session_effort.fit("xhigh", six) == "xhigh"
    assert session_effort.fit("minimal", ("low", "medium")) is None, "nothing below: the default"
    assert session_effort.fit("medium", ("minimal",)) == "minimal", "the highest rung the wire takes below it"


def test_fit_leaves_what_it_cannot_place_as_named() -> None:
    """No effort is no effort; a provider that cannot say leaves the word
    alone, the way the retry descent reads labels without ``wire_keys``; and a
    word this raven does not rank (a mode's catalogue may say one) goes out
    as named for the wire to answer."""
    four = ("minimal", "low", "medium", "high")
    assert session_effort.fit(None, four) is None
    assert session_effort.fit("", four) is None
    assert session_effort.fit("max", None) == "max"
    assert session_effort.fit("turbo", four) == "turbo"


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
