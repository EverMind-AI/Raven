"""submit_answer: serialized, adjudicated final-answer delivery.

Trace analysis across models keeps finding the same two post-computation
failures: a correctly computed result set gets retyped by hand and truncated
on the way out, and when several candidate readings were all computed, the
final pick happens silently with no recorded reason. Neither is a reasoning
failure -- both are delivery-path failures, so the fix lives in the delivery
path: one tool that takes the rows as data (not prose) and refuses to write
until the reading that produced them is committed with a reason.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.plugin.context import PluginContext

_MIN_REASON_CHARS = 40
_MAX_ROWS = 10_000
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_FOR_EACH_RE = re.compile(r"\bfor (?:each|every)\b", re.IGNORECASE)


class SubmitAnswerTool(Tool):
    """Write the final answer from a rows list plus an adjudication record."""

    timeout_seconds = 30.0

    def __init__(
        self,
        answer_path: str = "/workspace/answer.txt",
        question_path: str = "/workspace/dab_question.txt",
    ) -> None:
        self._answer_path = answer_path
        self._question_path = question_path
        self._for_each_warned = False

    @property
    def name(self) -> str:
        return "submit_answer"

    @property
    def description(self) -> str:
        return (
            "The sanctioned way to deliver the final answer. Pass the result as rows -- one "
            "element per item, taken from your computed result object (iterate it; never "
            "retype values by hand, and never summarize N items into fewer lines). Every "
            "value is carried whole as stored: a compound or list-valued field ('A, B, C') "
            "is delivered as the entire stored string, never trimmed to one element of it. "
            "When the question says 'for each X', rows carries one element per X in the "
            "data: a superlative (best/highest/latest) selects WITHIN each group, it never "
            "filters which groups appear. When the question itself asks for exactly the "
            "top k items, check the boundary before submitting: name the ranking metric "
            "and the margin between rank k and rank k+1 -- a tie or hairline margin there "
            "means re-reading which metric the question actually asks; this never applies "
            "to a 'for each'/'per group' question, where every group is still delivered. "
            "A delivered count or total must come from a "
            "computation over the full filtered population, never scaled up from a "
            "sample or a labeled subset. Commit the interpretation that produced it: "
            "selected_reading names the reading you stand behind, rejected_readings lists "
            "the alternatives you computed or considered, and reason must say what evidence "
            "in the data decided between them. Refuses to write when the adjudication is "
            "missing or hollow."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The answer, one item per element, serialized from the "
                    "computed result, each value carried whole (never a subset of a "
                    "compound value). A single-value answer is a one-element list.",
                },
                "selected_reading": {
                    "type": "string",
                    "description": "The interpretation of the question this answer commits to.",
                },
                "rejected_readings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alternative readings that were considered and why-summaries "
                    "belong in reason. Empty only if the question admits a single reading.",
                },
                "reason": {
                    "type": "string",
                    "description": "The evidence that decided the adjudication: which columns/"
                    "probes support the selected reading and what disqualified the others. "
                    "When the candidates are competing rules or explanations, evidence means "
                    "population base rates computed from the data -- for each candidate, how "
                    "many of ALL records trigger it (one triggered by nearly everything, or "
                    "nothing, is background noise, not the answer) -- and whether its scope "
                    "actually covers this case.",
                },
            },
            "required": ["rows", "selected_reading", "reason"],
        }

    async def execute(
        self,
        rows: list[str],
        selected_reading: str,
        reason: str,
        rejected_readings: list[str] | None = None,
    ) -> str:
        rows = [str(r).rstrip("\n") for r in (rows or []) if str(r).strip()]
        if not rows:
            return "[SUBMIT_ANSWER REFUSED] rows is empty; pass every item of the result."
        if len(rows) > _MAX_ROWS:
            return f"[SUBMIT_ANSWER REFUSED] {len(rows)} rows exceeds {_MAX_ROWS}."
        if len((reason or "").strip()) < _MIN_REASON_CHARS:
            return (
                "[SUBMIT_ANSWER REFUSED] reason is too thin to count as an adjudication; "
                "state the evidence (columns, probe results) that decided between readings."
            )
        if not (selected_reading or "").strip():
            return "[SUBMIT_ANSWER REFUSED] selected_reading is required."
        # One in-session refusal while the tools are still in hand beats any
        # post-hoc repair: a gate can reformat a delivered answer but cannot
        # compute the fields the agent never fetched. Refuse once, then defer
        # to the agent's judgment (some questions genuinely have few groups).
        if len(rows) < 3 and not self._for_each_warned:
            question = self._read_question()
            if question and _FOR_EACH_RE.search(question):
                self._for_each_warned = True
                return (
                    "[SUBMIT_ANSWER REFUSED] the question says 'for each' a group, yet "
                    f"only {len(rows)} rows are delivered. 'For each G' means one rows "
                    "element per G present in the data, each carrying every field the "
                    "question asks (names/titles included) -- a best/highest/latest "
                    "selects WITHIN each group, it never filters which groups appear. "
                    "Recompute the full per-group result, then submit again. If the data "
                    "truly contains fewer than 3 groups, resubmit as is."
                )
        # An adjudication between readings is only evidence-backed if it cites
        # measured quantities. Single-record inspection produces prose; probes
        # produce numbers -- population-wide counts or rates for each reading
        # (how often each candidate fires across ALL records, not just the
        # target) are what separates the real hit from plausible background.
        if rejected_readings and len(_NUMBER_RE.findall(reason or "")) < 2:
            return (
                "[SUBMIT_ANSWER REFUSED] the adjudication rejects alternative readings but "
                "cites no measured quantities. Run the probes and put their numbers in "
                "reason: population-wide counts or rates per candidate reading (a candidate "
                "that fires on nearly every record, or on none, is background, not the "
                "answer), then submit again."
            )

        import json

        path = Path(self._answer_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        record = {
            "selected_reading": selected_reading.strip(),
            "rejected_readings": [str(r) for r in (rejected_readings or [])],
            "reason": reason.strip(),
            "rows_delivered": len(rows),
        }
        adjudication = path.parent / "adjudication.json"
        adjudication.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return (
            f"wrote {len(rows)} rows to {path}; adjudication recorded. Re-read the question "
            "once against the delivered rows: every part answered, at the right grain, in the "
            "right unit and format?"
        )


    def _read_question(self) -> str:
        try:
            return Path(self._question_path).read_text(encoding="utf-8")
        except OSError:
            return ""


def make_submit_answer_tool(ctx: PluginContext) -> Tool:
    config = ctx.config or {}
    return SubmitAnswerTool(
        answer_path=str(config.get("answer_path") or "/workspace/answer.txt"),
        question_path=str(config.get("question_path") or "/workspace/dab_question.txt"),
    )
