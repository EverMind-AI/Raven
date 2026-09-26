"""A participant entry point is a factory for an instance carrying the selected methods, checked at binding."""

import pytest

from experimental.curator.raven_adapter.bind import conform
from experimental.curator.raven_adapter.observe import build_participant
from experimental.curator.raven_adapter.planning.contracts import PlanReader
from experimental.curator.raven_adapter.targets.action import TARGETS as ACTION
from experimental.curator.raven_adapter.targets.capability import TARGETS as CAPABILITY
from raven.contracts.participant import AgentParticipant, StepView
from raven.contracts.tool_gate import ToolGate

REVIEW = next(target for target in ACTION if target.name == "action.review")
SELECT = next(target for target in CAPABILITY if target.name == "capability.select_tools")


class Reviewer(AgentParticipant):
    async def review(self, step):
        return None


async def review(step):
    return None


def test_a_factory_returning_a_participant_with_the_selected_method_is_accepted():
    assert isinstance(build_participant(Reviewer, [REVIEW]), Reviewer)


def test_the_method_itself_is_refused_with_a_message_naming_the_target():
    with pytest.raises(
        TypeError, match="action.review: the entry point must be a factory taking no positional arguments"
    ):
        build_participant(review, [REVIEW])


def test_a_participant_missing_or_inheriting_the_selected_method_is_refused():
    with pytest.raises(TypeError, match="capability.select_tools: the selected method is not implemented"):
        build_participant(Reviewer, [SELECT])
    with pytest.raises(TypeError, match="the selected method is missing"):
        build_participant(object, [REVIEW])


class Gate:
    name = "gate"

    async def adjudicate(self, name, params, *, session_workdir=None):
        return None


class SyncGate:
    name = "gate"

    def adjudicate(self, name, params):
        return None


class NarrowGate:
    name = "gate"

    async def adjudicate(self, name, params=None):
        return None


def test_a_component_is_checked_against_the_way_the_host_calls_it():
    conform(Gate(), ToolGate)
    with pytest.raises(TypeError, match="must be async"):
        conform(SyncGate(), ToolGate)
    with pytest.raises(TypeError, match="does not accept the host's call"):
        conform(NarrowGate(), ToolGate)


def test_participant_targets_show_the_step_they_receive():
    assert StepView in REVIEW.knowledge and StepView in SELECT.knowledge


def test_action_side_targets_show_how_to_read_the_plan():
    gates = next(target for target in ACTION if target.name == "action.tool_gates")
    assert all(PlanReader in target.knowledge for target in (REVIEW, SELECT, gates))


class PlanAwareReviewer(AgentParticipant):
    def __init__(self, plan):
        self.plan = plan

    async def review(self, step):
        return None


def test_an_entry_point_may_declare_the_plan_reader_as_a_keyword_only_dependency():
    def reader():
        return {"stage": "intake"}

    def entry(*, plan):
        return PlanAwareReviewer(plan)

    def positional(plan):
        return PlanAwareReviewer(plan)

    assert build_participant(entry, [REVIEW], {"plan": reader}).plan() == {"stage": "intake"}
    with pytest.raises(TypeError, match="host-supplied keyword-only dependency"):
        build_participant(entry, [REVIEW])
    with pytest.raises(TypeError, match="no positional arguments"):
        build_participant(positional, [REVIEW], {"plan": reader})
