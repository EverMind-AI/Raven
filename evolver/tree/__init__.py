"""The evolver tree: node schema with its JSON round-trip (``node``), the tree
store and its views (``store``), and git-backed physical state (``git_ops``).
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
