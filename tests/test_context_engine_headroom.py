"""Reply headroom -- the one prompt bound every history strategy obeys.

Two halves. The arithmetic (a flat floor, clamped to the window) is asserted
across the eight tabulated window sizes, because the defect it replaces
was window-dependent: reserving the model's output ceiling costs a share that
grows as the window shrinks, so the table is where a regression would show.
The learning half is asserted against the refusal shapes a provider can
produce, because that is what pays for the flat floor.
"""

from __future__ import annotations

import pytest

from raven.config.raven import ContextCompactionConfig
from raven.context_engine.headroom import (
    DEFAULT_MIN_REPLY_HEADROOM_TOKENS,
    DEFAULT_OVERFLOW_BACKOFF_TOKENS,
    learn_from_overflow,
    max_prompt_tokens,
    reply_headroom,
    required_headroom,
    reset,
)

# The compaction trigger's upper ratio. Read from the config the trigger
# itself reads, not written out here: the load-time invariant relates it to
# this module's floor, and a copy of the number would go on asserting the old
# crossover after the band moved.
R_MAX = ContextCompactionConfig().trigger_ratio_max

# Every window the trigger table covers, 32K to 2M.
WINDOWS = (32_768, 65_536, 128_000, 200_000, 262_144, 512_000, 1_000_000, 2_000_000)

# Below this the static floor is larger than the slack the adaptive ratio would
# have left, so the floor decides the bound and the ratio never binds:
# 4096 / (1 - 0.90) = 40960.
FLOOR_GOVERNS_BELOW = int(DEFAULT_MIN_REPLY_HEADROOM_TOKENS / (1 - R_MAX))


@pytest.fixture(autouse=True)
def _forget_learned_headroom():
    reset()
    yield
    reset()


# ---------------------------------------------------------------------------
# The policy arithmetic, across the plan's windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", WINDOWS)
def test_the_reservation_is_flat_and_the_bound_is_the_rest(window: int) -> None:
    """The whole point: the reservation does not scale with the window, so a
    small window keeps the same absolute room for its reply as a large one and
    spends everything else on history."""
    assert reply_headroom("m", window=window) == DEFAULT_MIN_REPLY_HEADROOM_TOKENS
    assert max_prompt_tokens("m", window=window) == window - DEFAULT_MIN_REPLY_HEADROOM_TOKENS


@pytest.mark.parametrize("window", WINDOWS)
def test_the_bound_is_a_larger_share_of_a_smaller_window(window: int) -> None:
    """The inversion this replaces, stated as a monotonicity: reserving the
    ceiling made the prompt's share of the window *fall* as the window fell
    (50% of 32768 against a 16384 ceiling, 87.5% of 128000). Here the share
    only rises as the window grows, and never below 87%."""
    share = max_prompt_tokens("m", window=window) / window
    assert share >= 0.87
    assert share >= max_prompt_tokens("m", window=WINDOWS[0]) / WINDOWS[0]


@pytest.mark.parametrize("window", WINDOWS)
def test_which_of_the_floor_and_the_adaptive_ratio_governs(window: int) -> None:
    """The load-time invariant ``min_reply_headroom_tokens < W * (1 - r_max)``
    holds exactly when the adaptive trigger binds before the static floor.

    It does **not** hold at every tabulated window. At 32768 the slack above
    a 0.90 ratio is 3276 tokens, less than the 4096 floor, so the floor
    decides and the ratio is inert. The crossover is 40960.
    """
    invariant_holds = DEFAULT_MIN_REPLY_HEADROOM_TOKENS < window * (1 - R_MAX)
    adaptive_bound = int(R_MAX * window)
    floor_bound = max_prompt_tokens("m", window=window)

    assert invariant_holds == (adaptive_bound < floor_bound)
    assert invariant_holds == (window > FLOOR_GOVERNS_BELOW)


def test_a_window_below_the_floor_is_clamped_not_negative() -> None:
    assert reply_headroom("m", window=2_048) == 2_048
    assert max_prompt_tokens("m", window=2_048) == 1
    assert reply_headroom("m", window=0) == 0


def test_an_explicit_minimum_overrides_the_default() -> None:
    assert reply_headroom("m", window=128_000, minimum=32_000) == 32_000


# ---------------------------------------------------------------------------
# Learning from a refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def ceiling(monkeypatch):
    """Pin what the catalogue says the model may emit."""

    def _set(value: int) -> None:
        monkeypatch.setattr("raven.providers.rates.resolve_max_output_tokens", lambda *a, **k: value)

    return _set


def test_nothing_is_required_until_a_refusal_lands() -> None:
    assert required_headroom("m") == 0
    assert required_headroom("") == 0


def test_the_first_refusal_jumps_straight_to_the_models_ceiling(ceiling) -> None:
    """For the provider that validates ``prompt + ceiling <= window`` this is
    the whole answer, reached in one refusal rather than backed into: the
    request left 11072 free, less than the ceiling, so the ceiling explains it.
    """
    ceiling(16_384)

    assert learn_from_overflow("m", window=131_072, prompt_tokens=120_000) == 16_384
    assert reply_headroom("m", window=131_072) == 16_384
    # And the prompt the next turn hands out is one that ceiling fits beside.
    assert max_prompt_tokens("m", window=131_072) + 16_384 <= 131_072


def test_a_refusal_the_ceiling_cannot_explain_backs_off_past_what_was_tried(ceiling) -> None:
    """110000 of 131072 already left 21072 free -- more than the 16384 ceiling
    -- so this provider's bound is something else, and the only honest move is
    past the room that was just refused."""
    ceiling(16_384)

    learned = learn_from_overflow("m", window=131_072, prompt_tokens=110_000)

    assert learned == 21_072 + DEFAULT_OVERFLOW_BACKOFF_TOKENS


def test_a_second_refusal_always_moves(ceiling) -> None:
    """Otherwise a provider that keeps refusing would settle on one number and
    the retry budget would burn on identical prompts."""
    ceiling(16_384)
    first = learn_from_overflow("m", window=131_072, prompt_tokens=120_000)

    second = learn_from_overflow("m", window=131_072, prompt_tokens=118_000)

    assert second >= first + DEFAULT_OVERFLOW_BACKOFF_TOKENS


def test_the_reservation_never_takes_more_than_half_the_window(ceiling) -> None:
    """Past that the cure costs more history than the overflow did, and a
    model needing more than half its own window for a reply is one this loop
    cannot serve anyway."""
    ceiling(64_000)

    assert learn_from_overflow("m", window=32_768, prompt_tokens=30_000) == 16_384
    assert reply_headroom("m", window=32_768) == 16_384


def test_learning_is_per_model(ceiling) -> None:
    ceiling(16_384)
    learn_from_overflow("strict", window=131_072, prompt_tokens=120_000)

    assert required_headroom("strict") == 16_384
    assert required_headroom("lenient") == 0


def test_an_explicit_minimum_still_wins_when_it_is_larger(ceiling) -> None:
    ceiling(2_000)
    learn_from_overflow("m", window=131_072, prompt_tokens=130_000)

    assert required_headroom("m") == DEFAULT_OVERFLOW_BACKOFF_TOKENS
    assert reply_headroom("m", window=131_072, minimum=32_000) == 32_000


def test_reset_forgets_everything(ceiling) -> None:
    ceiling(16_384)
    learn_from_overflow("m", window=131_072, prompt_tokens=120_000)

    reset()

    assert required_headroom("m") == 0


# --------------------------------------------------------------------------- #
# the request's own max_tokens                                                 #
# --------------------------------------------------------------------------- #


def test_a_request_with_room_for_the_whole_ceiling_goes_out_as_before() -> None:
    from raven.context_engine.headroom import request_max_tokens

    assert request_max_tokens(window=200_000, prompt_tokens=100_000, ceiling=64_000, floor=4_096) is None


def test_a_request_near_the_window_is_sized_to_the_room_beside_its_prompt() -> None:
    from raven.context_engine.headroom import request_max_tokens

    # 200000 - 170000 * 1.05 = 21500, under the 64000 ceiling.
    assert request_max_tokens(window=200_000, prompt_tokens=170_000, ceiling=64_000, floor=4_096) == 21_500


def test_the_narrowed_request_never_asks_for_less_than_the_floor() -> None:
    from raven.context_engine.headroom import request_max_tokens

    assert request_max_tokens(window=200_000, prompt_tokens=199_000, ceiling=64_000, floor=4_096) == 4_096


def test_an_unknown_window_leaves_the_request_alone() -> None:
    from raven.context_engine.headroom import request_max_tokens

    assert request_max_tokens(window=0, prompt_tokens=10, ceiling=64_000, floor=4_096) is None


def test_a_configured_ceiling_below_the_floor_is_not_raised() -> None:
    from raven.context_engine.headroom import request_max_tokens

    assert request_max_tokens(window=16_000, prompt_tokens=15_000, ceiling=2_048, floor=4_096) == 2_048


def test_a_refused_request_that_was_already_narrowed_moves_the_table_by_one_step() -> None:
    reset()
    learned = learn_from_overflow("m", window=200_000, prompt_tokens=170_000, step=8_192, narrowed=True)
    assert learned == 8_192, "the room it asked for was the room left; the estimate ran short, not the ceiling"


def test_a_prompt_that_alone_fills_the_window_teaches_nothing_about_the_reply() -> None:
    reset()
    assert learn_from_overflow("m", window=32_000, prompt_tokens=40_000, step=8_192) == 0
    assert required_headroom("m") == 0
