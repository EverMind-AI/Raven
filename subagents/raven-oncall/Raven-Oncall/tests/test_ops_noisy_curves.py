"""Noisy training curves for the scripted world.

Real loss curves jitter, so a spike is not divergence. Every public monitoring
benchmark scripts deterministic conditions, which means an agent that alarms on
the first bad sample scores the same as one that waits for a trend -- the
distinction that matters most in production is the one they cannot measure.

These builders author the two curves the kill decision hinges on:
  - jitter that keeps descending  -> killing it is wrong
  - jitter that turns and climbs  -> killing it is right, and the earlier the better
"""

from __future__ import annotations

from raven.ops.scripted_backend import (
    JobScript,
    converging_curve,
    diverging_curve,
)


def _losses(script: JobScript) -> list[float]:
    return [sample["loss"] for _, sample in script.progress]


def test_a_converging_curve_ends_lower_than_it_started():
    script = converging_curve(samples=40, seed=1)
    losses = _losses(script)
    assert len(losses) == 40
    assert losses[-1] < losses[0]
    assert script.fail is False, "left alone it succeeds, so killing it is wrong"


def test_a_converging_curve_still_jitters_so_single_samples_mislead():
    losses = _losses(converging_curve(samples=40, seed=1))
    rises = sum(1 for a, b in zip(losses, losses[1:]) if b > a)
    assert rises >= 5, "a monotone curve would make the judgement trivial"


def test_a_diverging_curve_turns_upward_after_the_given_point():
    script = diverging_curve(samples=40, diverge_at=20, seed=2)
    losses = _losses(script)
    assert losses[19] < losses[0], "it descends before the turn"
    assert losses[-1] > losses[19], "and climbs after it"
    assert script.fail is True, "left alone it fails, so killing it is right"


def test_the_two_curves_are_indistinguishable_before_the_turn():
    conv = _losses(converging_curve(samples=40, seed=3))
    div = _losses(diverging_curve(samples=40, diverge_at=20, seed=3))
    assert conv[:20] == div[:20], (
        "sharing the pre-turn prefix is what forces the agent to wait for a "
        "trend instead of reacting to the first rise"
    )


def test_curves_are_deterministic_for_a_given_seed():
    assert _losses(converging_curve(samples=20, seed=7)) == _losses(
        converging_curve(samples=20, seed=7)
    )


def test_progress_offsets_advance_so_samples_reveal_over_campaign_time():
    script = converging_curve(samples=10, seed=1, interval_ms=60_000)
    offsets = [offset for offset, _ in script.progress]
    assert offsets == sorted(offsets)
    assert offsets[1] - offsets[0] == 60_000
    assert script.finish_after_ms == 10 * 60_000
