"""Grade one arm with the task's own automated check, and nothing else.

The pinchbench task grades itself in two halves: an **automated** check
that reads the report the run wrote, and an **LLM judge** that reads the
transcript. This bridge runs the first half and says plainly that the second
half did not run.

Why the second half is skipped rather than attempted: the judge calls
OpenRouter, and the judge that *cannot* be reached does not abstain — it
returns a score of zero with the error in its notes. A zero from a judge
that never ran is not a measurement, and averaging it into the score would
shrink every paired difference towards nothing while looking like a result.

So the number this harness reports is the **automated** component, which is
model-free and deterministic: the same report always scores the same, and
two arms differ only because the rung differed. The judge component is
reported as **not run**, in the same spirit as the rest of this project: a
check that could not run is not run, never a pass and never a zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from grading import _grade_automated
from raven_executor import _session_to_openclaw_transcript
from task_loader import Task


def session_messages(home: Path) -> list[dict]:
    """The raw messages of the run that used this agent home.

    An agent home holds the product's ACP sessions under
    ``subagent_sessions/<product>/acp/sessions/``. Each arm of a paired run
    gets its own home, so the newest session in **this** home is this
    arm's session and no other -- which is what makes the pairing honest.
    """
    # The sessions live under the product's ACP home, which the launcher
    # places by its own rules -- ``<home>/subagent_sessions/...`` by
    # default, or wherever ``RESEARCH_NG_ACP_HOME`` points. Matching the
    # shape rather than the path is what keeps this true for both.
    sessions = sorted(
        Path(home).rglob("*/sessions/*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    for path in reversed(sessions):
        rows = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if rows:
            return rows
    return []


def automated_grade(
    task: Task,
    workspace_path: str,
    *,
    home: Path | None = None,
) -> tuple[float, str]:
    """Score one arm on the task's automated check.

    The transcript matters: the task checks whether the run **used web
    search**, and it looks for the tool calls in the transcript. A run that
    searched and was graded with an empty transcript would be marked as
    having not searched, which is the run being graded wrongly rather than
    the run being wrong.

    Returns the score and a short note describing the breakdown, so a reader
    can see *which* criterion moved rather than only that the total did.
    """
    transcript = []
    if home is not None:
        transcript = _session_to_openclaw_transcript(session_messages(home))
    result = _grade_automated(task, {"transcript": transcript, "workspace": workspace_path})
    breakdown: dict[str, Any] = dict(result.breakdown or {})
    return result.score, json.dumps(breakdown, ensure_ascii=False, sort_keys=True)


__all__ = ["automated_grade"]
