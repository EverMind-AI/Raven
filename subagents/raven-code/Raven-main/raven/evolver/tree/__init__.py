"""Evolver tree subsystem.

Phase 1 (C1, this module): node schema + JSON round-trip.
Future C2: Git-backed physical state management.
Future C3: tree topology + traversal helpers.
"""

from . import git_ops
from .node import (
    SCHEMA_VERSION,
    AppliedPatch,
    CandidatePatch,
    EvalResult,
    HarnessNode,
    JudgeAnalysis,
    NodeStatus,
    PatchComponent,
    PerTaskResult,
    ProposedComponent,
    SourceEvidence,
)
from .store import EvolverTreeStore, TreeView

__all__ = [
    "SCHEMA_VERSION",
    "AppliedPatch",
    "CandidatePatch",
    "EvalResult",
    "EvolverTreeStore",
    "HarnessNode",
    "JudgeAnalysis",
    "NodeStatus",
    "PatchComponent",
    "PerTaskResult",
    "ProposedComponent",
    "SourceEvidence",
    "TreeView",
    "git_ops",
]
