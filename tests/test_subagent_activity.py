"""``raven.agent.subagent.activity``: the live index an instance's conversation
view reads is held by the turn answering, not by whichever lane registered last."""

from __future__ import annotations

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
