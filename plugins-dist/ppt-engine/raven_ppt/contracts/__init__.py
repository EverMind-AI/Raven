"""Data shapes passed between PPT layers. No logic, no IO.

Everything here is frozen and comparable so that a stage's output can be
snapshotted in a test and diffed, and so no layer can mutate another layer's
result in place.
"""

from raven_ppt.contracts.brief import DeckBrief, PageBudget, brief_path, load_brief, write_brief
from raven_ppt.contracts.build import BuildOutcome, PageSource
from raven_ppt.contracts.capability import Capabilities
from raven_ppt.contracts.deck import DeckPlan, PageSpec
from raven_ppt.contracts.findings import Finding, Severity, blocking, warnings
from raven_ppt.contracts.intake import (
    Errand,
    IntakePlan,
    Question,
    StatedBrief,
    intake_path,
    load_plan,
    write_plan,
)
from raven_ppt.contracts.outline import (
    Outline,
    PagePlan,
    load_outline,
    outline_path,
    write_outline,
)
from raven_ppt.contracts.profile import Profile, StageSpec
from raven_ppt.contracts.project import Project
from raven_ppt.contracts.rendered import PageSize, WordBox
from raven_ppt.contracts.stage import Backend, StageResult

__all__ = [
    "Backend",
    "BuildOutcome",
    "Capabilities",
    "DeckBrief",
    "DeckPlan",
    "Errand",
    "Finding",
    "IntakePlan",
    "PageBudget",
    "PageSize",
    "PageSource",
    "PageSpec",
    "Outline",
    "PagePlan",
    "Profile",
    "Project",
    "Question",
    "Severity",
    "StatedBrief",
    "StageResult",
    "StageSpec",
    "WordBox",
    "blocking",
    "intake_path",
    "load_plan",
    "write_plan",
    "brief_path",
    "load_brief",
    "load_outline",
    "outline_path",
    "warnings",
    "write_brief",
    "write_outline",
]
