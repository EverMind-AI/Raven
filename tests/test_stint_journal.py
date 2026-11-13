"""The append-only record a round leaves for the rounds after it."""

from __future__ import annotations

from pathlib import Path

from raven.stint.journal import (
    JOURNAL,
    JOURNAL_ROUNDS_IN_PROMPT,
    append_entry,
    journal_tail,
    round_heading,
)


def _journal(rounds: int) -> str:
    return "\n".join(
        f"{round_heading(index)}\n\n### Build\n\nround {index} from the builder\n" for index in range(1, rounds + 1)
    )


def test_a_round_past_nine_is_still_found_by_the_round_after_it() -> None:
    """The heading is how a round is located, and a plain number would put round
    10 between round 1 and round 2 for anything reading the file in order."""
    assert round_heading(1) == "## Round 01"
    assert round_heading(10) == "## Round 10"
    assert journal_tail(_journal(10), rounds=1).startswith(round_heading(10))


def test_a_journal_shorter_than_the_window_travels_whole() -> None:
    tail = journal_tail(_journal(JOURNAL_ROUNDS_IN_PROMPT))
    assert tail.startswith(round_heading(1))
    assert "round 2 from the builder" in tail


def test_only_the_last_rounds_reach_the_next_prompt() -> None:
    """Forty rounds of journal would otherwise be most of every prompt, and the
    rounds before the window are in the commit log."""
    tail = journal_tail(_journal(5))

    assert tail.startswith(round_heading(4))
    assert "round 3 from the builder" not in tail
    assert "round 5 from the builder" in tail
    assert journal_tail(_journal(5), rounds=4).startswith(round_heading(2))


def test_a_second_turn_for_one_role_is_added_rather_than_replacing_the_first(tmp_path: Path) -> None:
    """A role that ran twice did run twice, and the first account is the only
    record that it happened."""
    path = tmp_path / JOURNAL

    append_entry(path, 3, "Build", "first turn: the arena")
    append_entry(path, 3, "Build", "second turn: the camera")

    text = path.read_text(encoding="utf-8")
    assert text.count(round_heading(3)) == 1
    assert "first turn: the arena" in text
    assert text.index("first turn: the arena") < text.index("second turn: the camera")


def test_an_account_longer_than_the_budget_is_clipped_rather_than_dropped(tmp_path: Path) -> None:
    path = tmp_path / JOURNAL

    append_entry(path, 1, "Build", "x" * 500, limit=60)

    written = path.read_text(encoding="utf-8").split("### Build\n\n", 1)[1].strip()
    assert len(written) == 60
    assert written.startswith("x" * 59)
    assert written.endswith(chr(0x2026)), "the clip is marked, so a reader knows the account was cut"


def test_a_role_that_said_nothing_leaves_no_section(tmp_path: Path) -> None:
    """An empty subsection reads as a role that ran and reported nothing, which
    is a different thing from a role whose account never reached the file."""
    path = tmp_path / JOURNAL

    append_entry(path, 1, "Build", "   \n\n")
    append_entry(path, 1, "  ", "an account with nobody to attribute it to")

    assert not path.exists()


def test_an_account_for_a_round_nobody_opened_opens_it(tmp_path: Path) -> None:
    """The round's own section is normally cut first, but a stage that ran
    without one must not lose its account for it."""
    path = tmp_path / JOURNAL

    append_entry(path, 4, "Verifier", "the duel is two rounds now")

    text = path.read_text(encoding="utf-8")
    assert text.startswith(round_heading(4))
    assert text.rstrip().endswith("### Verifier\n\nthe duel is two rounds now")
