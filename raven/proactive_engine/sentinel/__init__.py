"""Sentinel — the LLM-planned path of the Proactive Engine.

The ProactivePlanner decides; the three nudge executors (plain nudge, inject,
defer) carry it out under NudgePolicy; RoutineLearner, NudgeFeedbackTracker,
ContextAssembler and ProactiveSpawn feed and follow it; SentinelRunner binds
them into a periodic tick loop.
"""

from raven.proactive_engine.sentinel.executor.defer_manager import DeferManager
from raven.proactive_engine.sentinel.executor.dispatcher import ExecutionResult, NudgeDispatcher
from raven.proactive_engine.sentinel.executor.injector import NudgeInjector
from raven.proactive_engine.sentinel.executor.runner import SentinelAssembly, SentinelRunner, TickOutcome
from raven.proactive_engine.sentinel.executor.spawn import ProactiveSpawn
from raven.proactive_engine.sentinel.feedback.tracker import (
    FeedbackSignal,
    NudgeFeedbackTracker,
    new_nudge_id,
)
from raven.proactive_engine.sentinel.planner import ProactivePlanner
from raven.proactive_engine.sentinel.predictor.context_assembler import ContextAssembler
from raven.proactive_engine.sentinel.predictor.routine_learner import RoutineLearner
from raven.proactive_engine.sentinel.trigger_policy.policy import CheckResult, NudgePolicy
from raven.proactive_engine.sentinel.trigger_policy.prefs import (
    PersonalizedOverrides,
    ProactivityPreferencesReader,
)
from raven.proactive_engine.sentinel.types import (
    ActiveSession,
    NudgePolicyState,
    PlannerContext,
    PlannerDecision,
    Routine,
)

__all__ = [
    "ActiveSession",
    "CheckResult",
    "ContextAssembler",
    "DeferManager",
    "ExecutionResult",
    "FeedbackSignal",
    "NudgeDispatcher",
    "NudgeFeedbackTracker",
    "NudgeInjector",
    "NudgePolicy",
    "NudgePolicyState",
    "PersonalizedOverrides",
    "PlannerContext",
    "PlannerDecision",
    "ProactivePlanner",
    "ProactiveSpawn",
    "ProactivityPreferencesReader",
    "Routine",
    "RoutineLearner",
    "SentinelAssembly",
    "SentinelRunner",
    "TickOutcome",
    "new_nudge_id",
]
