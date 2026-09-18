"""The append-only record a round leaves for the rounds after it."""

from __future__ import annotations

from pathlib import Path

from raven.stint.journal import (
    JOURNAL,
    JOURNAL_ROUNDS_IN_PROMPT,
    append_entry,
    begin_round,
    journal_tail,
    round_entry,
    round_heading,
)

TWO_ROLES = (
    "## Round 01\n\n"
    "### Dev\n\nbuilt the arena\n\n"
    "### QA\n\nthe arena boots, the camera does not\n\n"
    "## Round 02\n\n"
    "### Dev\n\nfixed the camera\n"
)


def _journal(rounds: int) -> str:
    return "\n".join(
        f"{round_heading(index)}\n\n### Dev\n\nround {index} from the developer\n" for index in range(1, rounds + 1)
    )


def test_a_round_past_nine_is_still_found_by_the_round_after_it() -> None:
    """The heading is how a round is located, and a plain number would put round
    10 between round 1 and round 2 for anything reading the file in order."""
    assert round_heading(1) == "## Round 01"
    assert round_heading(10) == "## Round 10"
    assert round_entry(f"{round_heading(10)}\n\n### Dev\n\nthe tenth round\n", 10) == "the tenth round"


def test_a_journal_shorter_than_the_window_travels_whole() -> None:
    tail = journal_tail(_journal(JOURNAL_ROUNDS_IN_PROMPT))
    assert tail.startswith(round_heading(1))
    assert "round 2 from the developer" in tail


def test_only_the_last_rounds_reach_the_next_prompt() -> None:
    """Forty rounds of journal would otherwise be most of every prompt, and the
    rounds before the window are in the commit log."""
    tail = journal_tail(_journal(5))

    assert tail.startswith(round_heading(4))
    assert "round 3 from the developer" not in tail
    assert "round 5 from the developer" in tail
    assert journal_tail(_journal(5), rounds=4).startswith(round_heading(2))


def test_a_role_reads_what_the_role_before_it_wrote_in_this_round() -> None:
    assert round_entry(TWO_ROLES, 1) == "built the arena"
    assert round_entry(TWO_ROLES, 1, "### QA") == "the arena boots, the camera does not"


def test_an_entry_stops_at_the_next_role_and_at_the_next_round() -> None:
    """Handing the Developer QA's paragraph as its own is how a role ends up
    reporting work it never did."""
    assert "arena boots" not in round_entry(TWO_ROLES, 1)
    assert "Round 02" not in round_entry(TWO_ROLES, 1, "### QA")
    assert round_entry(TWO_ROLES, 2) == "fixed the camera"


def test_asking_for_a_round_or_a_role_that_wrote_nothing_gives_nothing() -> None:
    assert round_entry(TWO_ROLES, 3) == ""
    assert round_entry(TWO_ROLES, 2, "### QA") == ""


def test_a_second_turn_for_one_role_is_added_rather_than_replacing_the_first(tmp_path: Path) -> None:
    """A role that ran twice did run twice, and the first account is the only
    record that it happened."""
    path = tmp_path / JOURNAL

    append_entry(path, 3, "Dev", "first turn: the arena")
    append_entry(path, 3, "Dev", "second turn: the camera")

    text = path.read_text(encoding="utf-8")
    assert text.count(round_heading(3)) == 1
    assert "first turn: the arena" in text
    assert text.index("first turn: the arena") < text.index("second turn: the camera")


def test_an_account_longer_than_the_budget_is_clipped_rather_than_dropped(tmp_path: Path) -> None:
    path = tmp_path / JOURNAL

    append_entry(path, 1, "Dev", "x" * 500, limit=60)

    written = round_entry(path.read_text(encoding="utf-8"), 1)
    assert len(written) == 60
    assert written.startswith("x" * 59)
    assert written.endswith(chr(0x2026)), "the clip is marked, so a reader knows the account was cut"


def test_a_role_that_said_nothing_leaves_no_section(tmp_path: Path) -> None:
    """An empty subsection reads as a role that ran and reported nothing, which
    is a different thing from a role whose account never reached the file."""
    path = tmp_path / JOURNAL

    append_entry(path, 1, "Dev", "   \n\n")
    append_entry(path, 1, "  ", "an account with nobody to attribute it to")

    assert not path.exists()


def test_the_rounds_own_section_is_opened_once_and_keeps_what_was_written_under_it(tmp_path: Path) -> None:
    begin_round(tmp_path, 1)
    append_entry(tmp_path / JOURNAL, 1, "Dev", "built the arena")
    begin_round(tmp_path, 1)
    begin_round(tmp_path, 2)

    text = (tmp_path / JOURNAL).read_text(encoding="utf-8")
    assert text.count(round_heading(1)) == 1
    assert "built the arena" in text
    assert text.index(round_heading(1)) < text.index(round_heading(2))
    assert round_entry(text, 1) == "built the arena"


def test_an_account_for_a_round_nobody_opened_opens_it(tmp_path: Path) -> None:
    """The round's own section is normally cut first, but a stage that ran
    without one must not lose its account for it."""
    path = tmp_path / JOURNAL

    append_entry(path, 4, "QA", "the duel is two rounds now")

    assert round_entry(path.read_text(encoding="utf-8"), 4, "### QA") == "the duel is two rounds now"
