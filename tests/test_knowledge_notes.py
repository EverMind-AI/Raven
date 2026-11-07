"""The channel a parser reports a problem on that did not stop it."""

from __future__ import annotations

import asyncio

from raven.knowledge._notes import collecting, joined, note


def test_a_note_outside_a_collector_goes_nowhere() -> None:
    """So a parser called directly -- by a test, by a script -- behaves exactly
    as it did before notes existed."""
    note("nobody is listening")  # must not raise


def test_notes_come_back_in_the_order_they_were_written() -> None:
    with collecting() as notes:
        note("first")
        note("second")

    assert notes == ["first", "second"]


def test_the_same_note_twice_is_one_note() -> None:
    """The natural way to write one is inside a loop over figures, and one
    sentence said forty times is a row nobody can read."""
    with collecting() as notes:
        for _ in range(40):
            note("a picture could not be read")

    assert notes == ["a picture could not be read"]


def test_a_nested_parse_does_not_clear_the_outer_collector() -> None:
    """A `.doc` is converted and handed to the Word parser, so one parse runs
    inside another."""
    with collecting() as outer:
        note("outer")
        with collecting() as inner:
            note("inner")
        note("outer again")

    assert inner == ["inner"]
    assert outer == ["outer", "outer again"]


def test_a_note_from_a_gathered_task_still_lands() -> None:
    """Which is the shape the figure loop has: descriptions are fetched
    concurrently, and a task started with `gather` copies the context. Mutating
    the list crosses that boundary; setting it would not."""

    async def run() -> list[str]:
        with collecting() as notes:

            async def worker(n: int) -> None:
                note(f"picture {n} could not be read")

            await asyncio.gather(*(worker(n) for n in range(3)))
        return notes

    assert asyncio.run(run()) == [f"picture {n} could not be read" for n in range(3)]


def test_one_line_for_a_row_is_capped_and_counts_the_rest() -> None:
    """It lands in a tooltip, and a file whose every figure failed differently
    would otherwise put a paragraph there."""
    assert joined([]) == ""
    assert joined(["one", "two"]) == "one two"
    assert joined([str(n) for n in range(6)], limit=2) == "0 1 (+4 more)"
