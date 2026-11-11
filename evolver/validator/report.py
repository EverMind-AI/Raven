"""The validator's verdict model: rule results per layer, case runs, judge verdicts, the report."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "fail", "advisory"]
Outcome = Literal["PASS", "FAIL", "STATIC-OK"]


class ValidatorInputError(Exception):
    """The command's inputs are unusable (exit code 2)."""


class ValidatorEnvironmentError(Exception):
    """The environment, not the folder, stopped the run (exit code 3)."""


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: Status
    message: str
    where: str = ""
    fix: str = ""


@dataclass
class LayerResult:
    layer: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(r.status == "fail" for r in self.results)


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    sha256: str
    text: str | None
    truncated: bool


@dataclass
class CaseRun:
    case_id: str
    reply: str
    full_text: str
    files: list[FileRecord]
    seconds: float
    error: str | None


@dataclass(frozen=True)
class CriterionVerdict:
    id: str
    verdict: Literal["pass", "fail", "not_applicable"]
    reason: str


@dataclass
class CaseVerdict:
    case_id: str
    passed: bool
    criteria: list[CriterionVerdict]
    raw: list[str]
    error: str | None


@dataclass
class Report:
    folder_digest: str
    suite_digest: str | None
    raven_version: str
    raven_commit: str | None
    agent_model: str
    judge_model: str
    rendered: dict[str, Any]
    layers: list[LayerResult]
    cases: list[CaseRun]
    verdicts: list[CaseVerdict]
    outcome: Outcome
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def exit_code(report: Report) -> int:
    return 1 if report.outcome == "FAIL" else 0
