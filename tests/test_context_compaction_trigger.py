"""The compaction trigger — where the line sits, and what moves it.

The defect this policy replaces was window-dependent: deducting the model's
output ceiling from the base before applying a percentage made the effective
trigger *fall* as the window fell, so the deployments with the least room
compacted the soonest. The table below is therefore the centre of the file --
a regression would show as a ratio that stops being monotone in the window, or
a trigger that stops being a share of the raw window.

The second half is the cap. The reply headroom bounds the trigger from above,
and on a small window it, not the ratio, is what decides -- which is a
property of the arithmetic worth pinning down, because the design note that
described the ratio as always binding is wrong below 51200.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from raven.config.raven import ContextCompactionConfig
from raven.context_engine.headroom import DEFAULT_MIN_REPLY_HEADROOM_TOKENS
from raven.context_engine.history.compacting.trigger import (
    CompactionTrigger,
    TriggerPolicy,
)

# Read from the config rather than written out, so a change to the band moves
# the crossover and the governance assertions with it instead of leaving them
# asserting the old number in a comment.
_R_MAX = ContextCompactionConfig().trigger_ratio_max

# Window, the ratio the design tabulates for it, and the trigger that follows
# from measuring that ratio against the raw window. Same eight windows the
# reply-headroom tests use, so the two halves of the bound stay comparable.
WINDOW_TABLE = [
    (32_768, 0.900, 29_491),
    (65_536, 0.900, 58_982),
    (128_000, 0.900, 115_200),
    (200_000, 0.881, 176_136),
    (262_144, 0.869, 227_796),
    (512_000, 0.840, 430_080),
    (1_000_000, 0.811, 811_026),
    (2_000_000, 0.800, 1_600_000),
]

# The same eight windows with the default 4096 headroom applied, and the target
# each compacts to: 40% of the raw window, lowered where that would reclaim less
# than ``minReclaimTokens`` below the trigger (only 32K, where the floor-capped
# trigger of 28672 sits 15565 above 13107).
TARGET_TABLE = [
    (32_768, 28_672, 12_288),
    (65_536, 58_982, 26_214),
    (128_000, 115_200, 51_200),
    (200_000, 176_136, 80_000),
    (262_144, 227_796, 104_857),
    (512_000, 430_080, 204_800),
    (1_000_000, 811_026, 400_000),
    (2_000_000, 1_600_000, 800_000),
]

# Above this the adaptive ratio binds; at or below it the static reply-headroom
# floor does and the ratio never gets a say: 4096 / (1 - 0.90).
FLOOR_GOVERNS_BELOW = int(DEFAULT_MIN_REPLY_HEADROOM_TOKENS / (1 - _R_MAX))


@pytest.fixture
def policy() -> TriggerPolicy:
    return TriggerPolicy(ContextCompactionConfig())


def _plan(policy: TriggerPolicy, *, window: int, prompt_tokens: int = 0, headroom: int | None = None):
    return policy.evaluate(
        prompt_tokens=prompt_tokens,
        window=window,
        reply_headroom=DEFAULT_MIN_REPLY_HEADROOM_TOKENS if headroom is None else headroom,
    )


# ---------------------------------------------------------------------------
# The ratio, across the windows the design tabulates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("window", "ratio", "_trigger"), WINDOW_TABLE)
def test_the_ratio_matches_the_tabulated_value(policy: TriggerPolicy, window: int, ratio: float, _trigger: int) -> None:
    assert round(policy.ratio(window), 3) == ratio


@pytest.mark.parametrize(("window", "_ratio", "trigger"), WINDOW_TABLE)
def test_the_unclamped_trigger_is_that_share_of_the_raw_window(
    policy: TriggerPolicy, window: int, _ratio: float, trigger: int
) -> None:
    """Raw window, not window-minus-a-reserve. Passing a headroom of zero
    isolates the ratio from the cap that the next section covers."""
    assert _plan(policy, window=window, headroom=0).trigger_tokens == trigger


@pytest.mark.parametrize(("window", "trigger", "target"), TARGET_TABLE)
def test_the_trigger_and_target_with_the_default_headroom(
    policy: TriggerPolicy, window: int, trigger: int, target: int
) -> None:
    """``min(ratio * W, W - headroom)`` and ``0.40 * W`` clamped to reclaim at
    least ``minReclaimTokens`` -- the whole formula, at every tabulated window."""
    plan = _plan(policy, window=window)
    assert (plan.trigger_tokens, plan.target_tokens) == (trigger, target)
    assert plan.trigger_tokens - plan.target_tokens >= ContextCompactionConfig().min_reclaim_tokens


def test_the_ratio_never_rises_as_the_window_grows(policy: TriggerPolicy) -> None:
    """The inversion this replaces, stated as a monotonicity. A large window
    can afford a smaller percentage because the incompressible floor is a
    smaller share of it; a small one cannot, and must trigger later."""
    ratios = [policy.ratio(window) for window, _, _ in WINDOW_TABLE]
    assert ratios == sorted(ratios, reverse=True)


def test_the_ratio_is_clamped_at_both_ends(policy: TriggerPolicy) -> None:
    cfg = ContextCompactionConfig()
    assert policy.ratio(1_024) == cfg.trigger_ratio_max
    assert policy.ratio(cfg.trigger_ref_window) == cfg.trigger_ratio_max
    assert policy.ratio(1 << 40) == cfg.trigger_ratio_min


def test_the_slope_is_per_doubling(policy: TriggerPolicy) -> None:
    """Two points, one doubling apart, differ by exactly the slope -- which is
    what makes the parameter readable without running the formula."""
    cfg = ContextCompactionConfig()
    assert math.isclose(policy.ratio(256_000) - policy.ratio(512_000), cfg.trigger_slope, abs_tol=1e-9)


def test_a_zero_window_does_not_divide_by_zero(policy: TriggerPolicy) -> None:
    """A window of zero reaches here only from a misconfigured or unknown
    model. Everything is over the line then, which is the safe answer; what
    matters is that the log2 does not raise on the way to it."""
    assert policy.ratio(0) == ContextCompactionConfig().trigger_ratio_max
    plan = _plan(policy, window=0, prompt_tokens=1)
    assert (plan.trigger_tokens, plan.target_tokens, plan.action) == (0, 0, "compact")


# ---------------------------------------------------------------------------
# The reply headroom as a cap -- and which of the two governs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("window", "_ratio", "_trigger"), WINDOW_TABLE)
def test_which_of_the_floor_and_the_adaptive_ratio_governs(
    policy: TriggerPolicy, window: int, _ratio: float, _trigger: int
) -> None:
    """The design's load-time invariant -- the floor is smaller than the slack
    above the ratio -- holds exactly when the ratio is the binding term.

    It does not hold at every tabulated window, which is the point: at 32768
    the slack above 0.90 is 3276 tokens against a 4096 floor, so the floor
    decides and the ratio is inert. The crossover is 40960.
    """
    plan = _plan(policy, window=window)
    invariant_holds = DEFAULT_MIN_REPLY_HEADROOM_TOKENS < window * (1 - _R_MAX)
    assert invariant_holds == (not plan.headroom_clamped)
    assert invariant_holds == (window > FLOOR_GOVERNS_BELOW)


def test_the_cap_is_the_window_less_the_headroom(policy: TriggerPolicy) -> None:
    plan = _plan(policy, window=32_768)
    assert plan.trigger_tokens == 32_768 - DEFAULT_MIN_REPLY_HEADROOM_TOKENS


def test_a_learned_headroom_pulls_the_trigger_in(policy: TriggerPolicy) -> None:
    """What makes an overflow non-recurring: the refusal raises the headroom,
    and the next turn's trigger is lower without anything else changing."""
    before = _plan(policy, window=200_000).trigger_tokens
    after = _plan(policy, window=200_000, headroom=64_000).trigger_tokens
    assert before == 176_136
    assert after == 136_000
    assert after < before


def test_the_trigger_never_reaches_the_whole_window(policy: TriggerPolicy) -> None:
    for window, _, _ in WINDOW_TABLE:
        assert _plan(policy, window=window).trigger_tokens < window


# ---------------------------------------------------------------------------
# The verdict, and the target it aims at
# ---------------------------------------------------------------------------


def test_a_prompt_over_the_line_compacts_and_one_on_it_does_not(policy: TriggerPolicy) -> None:
    """Strictly over: a prompt exactly on the trigger still fits, and paying
    for a compaction to reclaim nothing is the thrash the floor exists to
    avoid."""
    trigger = _plan(policy, window=200_000).trigger_tokens
    assert _plan(policy, window=200_000, prompt_tokens=trigger).action == "pass"
    assert _plan(policy, window=200_000, prompt_tokens=trigger + 1).action == "compact"


def test_the_target_is_a_share_of_the_raw_window(policy: TriggerPolicy) -> None:
    plan = _plan(policy, window=200_000, prompt_tokens=190_000)
    assert plan.target_tokens == int(ContextCompactionConfig().retain_ratio * 200_000)
    assert plan.reclaim_clamped is False


def test_the_target_drops_until_one_compaction_is_worth_running(policy: TriggerPolicy) -> None:
    """On a small window the plain target is too close to the trigger to buy
    much, so it is lowered until the round trip reclaims ``minReclaimTokens``.
    Without it a session at the line would compact every single turn."""
    cfg = ContextCompactionConfig()
    plan = _plan(policy, window=32_768, prompt_tokens=32_000)
    assert plan.reclaim_clamped is True
    assert plan.trigger_tokens - plan.target_tokens == cfg.min_reclaim_tokens
    assert plan.target_tokens < int(cfg.retain_ratio * 32_768)


def test_the_target_stops_at_zero_rather_than_going_negative(policy: TriggerPolicy) -> None:
    """A window this small cannot host the policy at all. The strategy still has
    to produce a turn, so the target bottoms out and the retention ranges --
    which never give up the head, the tail or a pinned body -- are what
    actually decides what survives."""
    plan = _plan(policy, window=8_192, prompt_tokens=8_000)
    assert plan.target_tokens == 0
    assert plan.reclaim_clamped is True


def test_the_reason_travels_with_the_verdict(policy: TriggerPolicy) -> None:
    """Pressure and overflow run the same arithmetic; telling them apart in a
    trace is the only way to see that a provider is refusing prompts the
    strategy thought were fine."""
    plan = policy.evaluate(
        prompt_tokens=1,
        window=200_000,
        reply_headroom=4_096,
        trigger=CompactionTrigger.OVERFLOW,
    )
    assert plan.trigger is CompactionTrigger.OVERFLOW
    assert str(plan.trigger) == "overflow"
    assert _plan(policy, window=200_000).trigger is CompactionTrigger.PRESSURE


def test_the_inputs_travel_with_the_verdict(policy: TriggerPolicy) -> None:
    plan = _plan(policy, window=200_000, prompt_tokens=190_000)
    assert (plan.prompt_tokens, plan.window) == (190_000, 200_000)
    assert plan.ratio == policy.ratio(200_000)


# ---------------------------------------------------------------------------
# Load-time validation -- a misconfigured policy fails before the first turn
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_a_target_at_or_above_the_trigger_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="retainRatio"):
            ContextCompactionConfig(retain_ratio=0.85)

    def test_a_tail_larger_than_the_target_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="tailKeepRatio"):
            ContextCompactionConfig(tail_keep_ratio=0.5)

    def test_an_inverted_ratio_band_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="triggerRatioMin"):
            ContextCompactionConfig(trigger_ratio_min=0.95)

    def test_a_ratio_above_one_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ContextCompactionConfig(trigger_ratio_max=1.2)

    def test_the_headroom_floor_is_not_validated_against_the_ratio(self) -> None:
        """The relation ``minTokens < window * (1 - triggerRatioMax)`` fails at
        every window at or below 40960, so enforcing it at load would refuse to
        start exactly the deployments the policy helps most. It is a property
        of the window, recorded in the plan above, not a config error."""
        cfg = ContextCompactionConfig()
        assert DEFAULT_MIN_REPLY_HEADROOM_TOKENS > 32_768 * (1 - cfg.trigger_ratio_max)
