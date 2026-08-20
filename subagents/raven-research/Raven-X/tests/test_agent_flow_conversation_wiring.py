"""Where the multi-turn surface attaches, and the proof that it does not.

``tests/test_agent_flow_conversation.py`` tests the parts in isolation. This file
tests the seams, because the parts being correct is not what protects the
measurement - the failure this repo keeps paying for is a capability that is
correct, on by default, and wired to a site nothing reaches, or wired to a site
the anchor also runs. So the assertions below are mostly negative: with the knob
off, the assembly must be the same object graph it was before, and the tool must
reset the same fields it always reset.
"""

from __future__ import annotations

import pytest

from raven.agent.flow.conversation import GatedHook
from raven.agent.flow.dr import build_dr_flow
from raven.agent.search_saturation import SearchSaturation
from raven.agent.tools.web import WebSearchTool
from raven.config.raven import DRFlowConfig


class _StubProvider:
    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no provider call belongs in an assembly test")


def _assembly(**conversation):
    config = DRFlowConfig(enabled=True, conversation=conversation) if conversation else DRFlowConfig(enabled=True)
    return build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)


# --------------------------------------------------------------------------- #
# Off means absent                                                            #
# --------------------------------------------------------------------------- #


def test_the_default_assembly_carries_no_conversation_surface_at_all():
    a = _assembly()
    assert a.conversation_enabled is False
    assert a.conversation_gate is None
    assert a.research_memo is False
    assert a.identity_scope == "turn"


def test_with_the_feature_off_no_observer_is_wrapped():
    """The wrapper is the whole behavioural difference, so its absence is the
    statement that a measured arm runs the pre-dr@3.0 hook chain."""
    a = _assembly()
    assert a.observers
    assert not any(isinstance(o, GatedHook) for o in a.observers)


def test_identity_scope_cannot_be_set_without_enabling_the_feature():
    """A knob whose own section says the feature is off must not move tool behaviour.

    ``identityScope`` is the one field here that reaches a measured code path -
    the web tool's per-turn reset - so a config that set it while leaving
    ``enabled`` false would change what a bench arm's second turn sees while
    every other signal said the surface was off. It is forced back rather than
    rejected because rejecting it would turn a harmless stale config into a
    startup failure.
    """
    a = _assembly(enabled=False, identity_scope="topic")
    assert a.identity_scope == "turn"


# --------------------------------------------------------------------------- #
# On means attached everywhere                                                #
# --------------------------------------------------------------------------- #


def test_enabling_the_feature_wraps_every_observer_not_a_chosen_few():
    """Wrapped after the list is complete, so a later observer cannot forget to opt in."""
    a = _assembly(enabled=True)
    assert a.observers
    assert all(isinstance(o, GatedHook) for o in a.observers)


def test_the_wrapper_preserves_observer_order():
    """Observer order is part of the flow contract - a restart has to be
    intercepted before the terminal gates see it - and wrapping must not be a
    place where that order can quietly change."""
    plain = [type(o).__name__ for o in _assembly().observers]
    wrapped = [type(o.inner).__name__ for o in _assembly(enabled=True).observers]
    assert plain == wrapped


@pytest.mark.parametrize("mode,has_gate", [("agentic", True), ("always", False)])
def test_only_the_agentic_mode_builds_a_classifier(mode, has_gate):
    """``always`` is the pre-dr@3.0 behaviour made explicit, and it must not spend
    a model call per turn to reproduce it."""
    a = _assembly(enabled=True, gate=mode)
    assert (a.conversation_gate is not None) is has_gate


def test_memo_limits_reach_the_assembly_rather_than_defaulting_silently():
    a = _assembly(enabled=True, memo_max_chars=111, memo_max_sources=3, memo_max_queries=4)
    assert (a.memo_max_chars, a.memo_max_sources, a.memo_max_queries) == (111, 3, 4)


def test_the_memo_can_be_switched_off_while_the_gate_stays_on():
    a = _assembly(enabled=True, research_memo=False)
    assert a.research_memo is False and a.conversation_gate is not None


# --------------------------------------------------------------------------- #
# Identity scope at the tool                                                  #
# --------------------------------------------------------------------------- #


def _tool(**kw):
    return WebSearchTool(api_key="k", **kw)


def test_the_default_turn_scope_clears_the_identity_sets():
    tool = _tool()
    tool._result_seen.add("https://a.example")
    tool._snippet_seen.add("d1")
    tool.start_turn()
    assert not tool._result_seen and not tool._snippet_seen


def test_topic_scope_keeps_identities_but_still_clears_every_budget():
    """The distinction the scope rests on: identities are knowledge, the rest are
    decisions. Carrying a decision across a turn boundary would let one turn close
    search for a question that had not been asked yet."""
    sat = SearchSaturation(k=1, on_saturate="stop")
    tool = _tool(saturation=sat)
    tool._result_seen.add("https://a.example")
    tool._snippet_seen.add("d1")
    tool._prior["q"] = ("cached",)
    tool._searches = 7
    sat.observe([])
    assert sat.stopped

    tool.start_turn(keep_identities=True)

    assert tool._result_seen == {"https://a.example"}
    assert tool._snippet_seen == {"d1"}
    assert not tool._prior
    assert tool._searches == 0
    assert not sat.stopped


def test_topic_scope_keeps_the_saturation_identity_set_but_not_its_streak():
    """The bounded part of the documented cost.

    A follow-up whose first searches legitimately re-find the previous turn's
    pages accumulates a dry streak it did not earn; resetting the streak caps that
    head start at ``k`` searches instead of letting it arrive pre-tripped.
    """
    sat = SearchSaturation(k=3, on_saturate="stop")
    sat.observe(["https://a.example"])
    sat.observe([])
    sat.observe([])
    assert sat.counters()["sat_dry_streak"] == 2

    sat.reset(keep_seen=True)

    assert sat.counters()["sat_seen"] == 1
    assert sat.counters()["sat_dry_streak"] == 0
    # And the retained identity still counts as seen, which is the point.
    sat.observe(["https://a.example"])
    assert sat.counters()["sat_dry_streak"] == 1


def test_a_plain_reset_still_clears_everything():
    """The measured path, asserted so that adding the parameter cannot have
    quietly changed the default."""
    sat = SearchSaturation(k=2)
    sat.observe(["https://a.example"])
    sat.reset()
    assert sat.counters()["sat_seen"] == 0
