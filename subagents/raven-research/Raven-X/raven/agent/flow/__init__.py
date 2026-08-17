"""Agent flows — versioned task-mode assemblies over the agent loop.

A flow is a frozen set of {observers, prompt section, tool shaping,
budgets} attached to the loop's existing seams; the loop itself stays a
task-agnostic engine. First resident: the deep-research flow (dr@1).
"""

from raven.agent.flow.answer_text import visible_answer
from raven.agent.flow.budget_note import BudgetNoteObserver
from raven.agent.flow.dr import DRFlowAssembly, DRModeSegmentBuilder, build_dr_flow
from raven.agent.flow.fetch_floor import FetchFloorObserver
from raven.agent.flow.final_shape import ShapedAnswer, shape_final_answer
from raven.agent.flow.finalize import ForcedFinalizeGate
from raven.agent.flow.spin_breaker import SpinEntryBreaker
from raven.agent.flow.verify import DraftReviewerGate

__all__ = [
    "BudgetNoteObserver",
    "DRFlowAssembly",
    "DRModeSegmentBuilder",
    "DraftReviewerGate",
    "FetchFloorObserver",
    "ForcedFinalizeGate",
    "ShapedAnswer",
    "SpinEntryBreaker",
    "build_dr_flow",
    "shape_final_answer",
    "visible_answer",
]
