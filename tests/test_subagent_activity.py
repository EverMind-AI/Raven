"""``raven.agent.subagent.activity``: the live index an instance's conversation
view reads is held by the turn answering, not by whichever lane registered last."""

from __future__ import annotations

import time

from raven.agent.subagent import activity

KEY = ("s1", "Raven-Research-NG", "raven-research-ng-b3b872")


def test_the_first_lane_to_register_keeps_the_instance_slot():
    with activity.collecting(instance=KEY) as answering:
        with activity.collecting(instance=KEY) as queued:
            assert activity.live_instance(*KEY) is answering, "a second lane never unseats the first"
            assert queued is not answering
        assert activity.live_instance(*KEY) is answering, "and its exit releases nothing it did not own"
    assert activity.live_instance(*KEY) is None


def test_watching_instance_registers_late_and_releases_only_its_own():
    with activity.collecting(live_key="spawn-1") as run:
        assert activity.live_instance(*KEY) is None, "a block opened without the instance is not indexed by it"
        with activity.watching_instance(run, KEY):
            assert activity.live_instance(*KEY) is run
        assert activity.live_instance(*KEY) is None
    with activity.collecting(instance=KEY) as answering:
        with activity.watching_instance(activity.RunActivity(), KEY):
            assert activity.live_instance(*KEY) is answering
        assert activity.live_instance(*KEY) is answering


def test_the_turn_starts_when_the_slot_is_taken_not_when_collection_opened():
    """A spawn builds its activity, then waits on ``hold_handle``. Only the lane
    that takes the lock reaches the slot, so the wait is not part of its turn --
    and a clock drawn from the build time opened at however long the spawn had
    queued behind the turn before it."""
    run = activity.RunActivity()
    assert run.turn_started_at_ms is None, "not answering an instance yet"
    queued_for_ms = 40
    time.sleep(queued_for_ms / 1000)

    with activity.watching_instance(run, KEY):
        assert run.turn_started_at_ms is not None
        # The stamp is the acquisition, so the wait is excluded. Compared as a
        # floor rather than an equality: the clock is the wall clock and the
        # sleep is a minimum, not a promise.
        assert run.turn_started_at_ms - run.started_at_ms >= queued_for_ms - 5


def test_a_lane_that_never_takes_the_slot_is_never_stamped():
    """Absent is what says an instance is answering nothing, so a lane that lost
    the race must not look like one that is working."""
    with activity.collecting(instance=KEY):
        loser = activity.RunActivity()
        with activity.watching_instance(loser, KEY):
            assert loser.turn_started_at_ms is None
