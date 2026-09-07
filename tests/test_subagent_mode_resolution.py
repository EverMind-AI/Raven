"""The session tier as one dispatch sees it: the clamp, then the precedence chain."""

from __future__ import annotations

import pytest

from raven.agent.subagent.mode_tiers import clamp_tier, turn_tier, turn_tier_in_force


@pytest.mark.parametrize(
    ("tier", "offered", "expected"),
    [
        ("high", ("medium", "high", "max"), "high"),
        ("max", ("medium", "high", "max"), "max"),
        ("max", ("medium", "high"), "high"),
        ("max", ("medium",), "medium"),
        ("high", ("medium",), "medium"),
        ("medium", ("high", "max"), "high"),
        ("high", ("max",), "max"),
        ("high", (), None),
        ("high", ("deep", "ultra"), None),
        ("deep", ("fast", "deep", "ultra"), None),
        ("", ("medium", "high", "max"), None),
        ("high", ("high", "medium", "max"), "high"),
    ],
)
def test_the_clamp_lands_on_the_nearest_rung_preferring_cheaper(tier, offered, expected):
    assert clamp_tier(tier, offered) == expected


def test_a_tier_outside_the_ladder_is_never_approximated():
    """`deep` is not "nearest" to `medium`; it is a word from another vocabulary.

    Guarding this is the whole reason the ladder is a constant rather than the
    catalogue's key order: a deployment that renamed its modes must fall through
    to the agent's own default, not be clamped onto an unrelated id.
    """
    assert clamp_tier("ultra", ("medium", "high", "max")) is None


def test_a_foreign_vocabulary_shares_no_rung_and_is_declined():
    """raven-research advertises its own `fast`/`deep`/`ultra` catalogue. Now
    that our ladder's cheapest rung is `medium`, the two vocabularies share no
    word at all, so every tier falls through on rule 2 (no shared rung) -- not
    on the subset guard this project tried and then removed as redundant.
    """
    # Empty intersection, not a removed guard: nothing here is a ladder member.
    for tier in ("medium", "high", "max"):
        assert clamp_tier(tier, ("fast", "deep", "ultra")) is None


def test_an_illegible_menu_is_never_climbed_towards():
    """The shape raven-code ships: a rung below `medium` that the ladder cannot rank,
    beside one it can. MR !472 was going to give it `low` / `max` and was closed
    unmerged; the catalogue landed by another route in `669c913a4` as
    `low` / `high` / `max`, so this is the live fleet and not a hypothetical.

    Raising to the cheapest *visible* rung would put a `medium` session on `max`
    -- the dearest thing the agent has, chosen because the genuinely cheaper
    `low` is invisible from here. On a control meant to cap effort that is the
    worst direction to be wrong in, so an unrankable menu falls through to the
    agent's own default. An exact hit still stands: `max` is `max` in any
    vocabulary that spells it the same.
    """
    assert clamp_tier("medium", ("low", "max")) is None
    assert clamp_tier("high", ("low", "max")) is None
    assert clamp_tier("max", ("low", "max")) == "max"

    # The menu raven-code actually advertises, so this case is measured against the
    # fleet rather than against the shape it was written from.
    assert clamp_tier("medium", ("low", "high", "max")) is None, "climbing past an unrankable low"
    assert clamp_tier("high", ("low", "high", "max")) == "high", "an exact hit is unaffected"
    assert clamp_tier("max", ("low", "high", "max")) == "max"


def test_the_climb_still_happens_when_the_whole_menu_is_legible():
    """The fallback is gated on legibility, not removed: every rung here is a
    ladder member, so the cheapest one really is the cheapest on offer."""
    assert clamp_tier("medium", ("high", "max")) == "high"
    assert clamp_tier("high", ("max",)) == "max"


def test_a_menu_with_all_three_tiers_plus_a_foreign_extra_still_clamps():
    """The shape a subset guard would have refused outright: an agent that
    offers the whole ladder plus one id of its own. Every fork under
    subagents/ is expected to converge on medium/high/max, so an agent adding
    a rung on top (`turbo`) is a natural, desirable menu, not a hazard -- it
    must still clamp tier for tier rather than being declined for not
    matching the ladder exactly.
    """
    offered = ("medium", "high", "max", "turbo")
    assert clamp_tier("medium", offered) == "medium"
    assert clamp_tier("high", offered) == "high"
    assert clamp_tier("max", offered) == "max"


from types import SimpleNamespace

from raven.agent.subagent.manager import SubagentManager


def _manager(tier: str, menus: dict[str, tuple[str, ...]]) -> SubagentManager:
    """A manager with the tier reader bound and `agent_modes` stubbed.

    Built with `__new__`: the real constructor wants a provider and a workspace,
    and none of the resolution path touches either.
    """
    mgr = SubagentManager.__new__(SubagentManager)
    mgr._instance_modes = {}
    mgr._session_tier = lambda _key: tier
    mgr.agent_modes = lambda agent: tuple(  # type: ignore[method-assign]
        SimpleNamespace(id=rung) for rung in menus.get(agent, ())
    )
    return mgr


def test_the_tier_applies_to_a_spawn_that_names_no_instance():
    """The case the old short-circuit dropped: `instance` is falsy on most spawns."""
    mgr = _manager("max", {"coder": ("medium", "high", "max")})
    assert mgr.resolve_mode("s1", "coder", None) == "max"


def test_the_tier_is_clamped_to_what_the_agent_offers():
    mgr = _manager("max", {"coder": ("medium", "high")})
    assert mgr.resolve_mode("s1", "coder", None) == "high"


def test_an_agent_sharing_no_rung_runs_on_its_own_default():
    mgr = _manager("max", {"researcher": ("deep", "ultra")})
    assert mgr.resolve_mode("s1", "researcher", None) is None


def test_an_instance_override_beats_the_tier():
    mgr = _manager("medium", {"coder": ("medium", "high", "max")})
    mgr._instance_modes[("s1", "coder", "inst")] = "max"
    assert mgr.resolve_mode("s1", "coder", "inst") == "max", "a narrower statement wins"


def test_a_manager_with_no_tier_reader_behaves_exactly_as_before():
    mgr = _manager("", {"coder": ("medium", "high", "max")})
    mgr._session_tier = None
    assert mgr.resolve_mode("s1", "coder", None) is None


def test_a_tier_switched_mid_turn_does_not_reach_that_turns_later_dispatches():
    """The documented boundary: a switch lands on the next turn, not this one.

    Reading the policy at dispatch time instead of from the turn's snapshot let a
    `session/set_mode` arriving between two sub-agent calls change the second one.
    """
    live = {"mode": "medium"}
    mgr = _manager("unused", {"coder": ("medium", "high", "max")})
    mgr._session_tier = lambda _key: live["mode"]

    with turn_tier(live["mode"]):
        first = mgr.resolve_mode("s1", "coder", None)
        live["mode"] = "max"
        second = mgr.resolve_mode("s1", "coder", None)

    assert first == "medium"
    assert second == "medium", "the turn keeps the tier it started on"
    assert mgr.resolve_mode("s1", "coder", None) == "max", "and the next turn sees the switch"


def test_outside_a_turn_the_live_policy_is_what_there_is():
    """No turn scope -- a direct-chat dispatch, a test rig -- still resolves."""
    assert turn_tier_in_force() is None
    mgr = _manager("high", {"coder": ("medium", "high", "max")})
    assert mgr.resolve_mode("s1", "coder", None) == "high"


def test_a_turn_that_starts_with_no_tier_dispatches_without_one():
    """An empty bind is a turn that had no tier, not an absent scope."""
    mgr = _manager("max", {"coder": ("medium", "high", "max")})
    with turn_tier(""):
        assert mgr.resolve_mode("s1", "coder", None) is None


def test_the_declined_log_names_the_side_that_actually_declined():
    """One sentence per decline, and no decline borrowing another's wording.

    `resolve_tier` refuses for three different reasons and the line said "offers no
    tier from medium/high/max" for all of them -- so an agent offering the complete
    ladder was named as the one at fault when the session's vocabulary was the foreign
    one, and an agent offering `low`/`max` was told it offered no tier while plainly
    offering `max`. A diagnostic that can assert the opposite of the truth sends the
    next reader to the wrong place. First two reported by 0xKT on !474; the third
    reported on !508 by a verifier, and filed there as pre-existing.

    The caller renders `decline.value` and tests no condition of its own, so a fourth
    reason cannot arrive wearing a third one's sentence.
    """
    from loguru import logger

    # loguru writes to its own sinks, not the stdlib logging `caplog` hooks -- the
    # same shape `tests/test_acp_server.py` uses.
    def said_for(tier: str, menu: tuple[str, ...], key: str) -> str:
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
        try:
            _manager(tier, {"coder": menu}).resolve_mode(key, "coder", None)
        finally:
            logger.remove(sink)
        return "\n".join(lines)

    foreign = said_for("deep", ("medium", "high", "max"), "s1")
    assert "deep" in foreign, "name the tier that could not be ranked"
    assert "offers no tier" not in foreign, "the agent offers all three; it is not the one declining"

    no_overlap = said_for("high", ("fast", "deep"), "s2")
    assert "offers no tier" in no_overlap, "here the menu really is the reason"

    illegible = said_for("medium", ("low", "max"), "s3")
    assert "offers no tier" not in illegible, "it offers max; saying otherwise is false"
    assert "low/max" in illegible, "show the menu that could not be ranked"
    assert "cannot rank" in illegible, "and say that ranking, not overlap, is what failed"
