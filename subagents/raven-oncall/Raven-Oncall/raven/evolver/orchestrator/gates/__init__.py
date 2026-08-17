"""Post-confirm gates (SOP §2 ⑥): infra health, activation, and paired lift.

Also home to the pluggable decision policy (``policy``/``strategies``) and the
focused-Fisher statistics (``fisher``) that let the two benchmark lines share
one round loop.
"""

from __future__ import annotations

from raven.evolver.orchestrator.gates.policy import (
    Baseline,
    BaselineProvider,
    CandidateOutcome,
    DecisionContext,
    FrozenColdStartBaseline,
    GatePolicy,
    PerParentFrozenBaseline,
    SameSessionPairedBaseline,
)
from raven.evolver.orchestrator.gates.strategies import (
    FocusedFisherGate,
    PairedTwoSigmaGate,
    confirm_job_name,
)

__all__ = [
    "Baseline",
    "BaselineProvider",
    "CandidateOutcome",
    "DecisionContext",
    "GatePolicy",
    "FrozenColdStartBaseline",
    "PerParentFrozenBaseline",
    "SameSessionPairedBaseline",
    "FocusedFisherGate",
    "PairedTwoSigmaGate",
    "confirm_job_name",
]
