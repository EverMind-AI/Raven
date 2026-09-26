"""Filter a round's signals, interpret the rest, and record the resulting feedback."""

import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from uuid import uuid4

from ..curator.generation.context.render import tool
from ..curator.harness.declaration import schema_for
from ..curator.raven_adapter.materialize import _write
from ..curator.raven_adapter.observe import plain
from ..iteration.exchange import exchange, messages
from .feedback import Feedback
from .materials import RECORDS, materials, read_records, record_tool

NAME = "submit_feedback"
_PROMPT = Path(__file__).resolve().parent / "prompts" / "analyst.md"


@dataclass(frozen=True)
class Limits:
    max_calls: int = 4
    call_timeout: float = 120

    def __post_init__(self):
        if (
            not isinstance(self.max_calls, int)
            or self.max_calls < 1
            or not isfinite(self.call_timeout)
            or self.call_timeout <= 0
        ):
            raise ValueError("analyst limits must be finite positive bounds")


def triage(signals) -> Feedback | None:
    """Decide without the model when the signals plainly say nothing needs analysis."""
    if not signals:
        return Feedback(decision="continue", reason="No evaluator produced a signal this round.")
    if all(signal.satisfied is True and not signal.text.strip() for signal in signals):
        return Feedback(decision="continue", reason="Every evaluator is satisfied and none added a remark.")
    return None


async def analyse(
    worker,
    provider,
    signals,
    sessions,
    *,
    previous_signals=(),
    previous_feedback=None,
    history=(),
    model=None,
    limits=Limits(),
) -> Feedback:
    """Produce this round's feedback; `curate` decisions are what the caller hands to workflow.improve."""
    trace = []
    record = {"signals": plain(signals), "trace": trace}
    try:
        feedback = triage(signals)
        if feedback is None:
            inspection = await worker.inspect()
            skills = sorted(name for name in inspection.sources if name.startswith("skill."))
            packet = materials(
                worker.baseline.task.text,
                signals,
                sessions,
                previous_plan=worker.last_plan,
                previous_signals=previous_signals,
                previous_feedback=previous_feedback,
                skills=skills,
                history=history,
            )
            if getattr(worker, "children", None):
                packet["composition"] = inspection.facts.get("composition", {})
                packet["child_expectations"] = {
                    name: child.plan.model_dump(mode="json") if child.plan else None
                    for name, child in worker.children.items()
                }
            locations = ["root", *(f"child/{name}" for name in getattr(worker, "children", {}))]
            packet["locations"] = locations
            schema = schema_for(Feedback)
            schema["$defs"]["Requirement"]["properties"]["locations"]["items"]["enum"] = locations

            def accept(arguments):
                parsed = Feedback.model_validate(arguments)
                if any(set(item.locations) - set(locations) for item in parsed.requirements):
                    raise ValueError("feedback names a location outside the supplied deployment")
                return parsed

            record["materials"] = packet
            _, feedback = await exchange(
                provider,
                messages(_PROMPT.read_text(), packet),
                [
                    record_tool(sessions),
                    tool(NAME, "Submit this round's decision and requirements.", schema),
                ],
                submit={NAME: accept},
                query={RECORDS: lambda arguments: read_records(sessions, arguments)},
                model=model,
                max_calls=limits.max_calls,
                timeout=limits.call_timeout,
                trace=trace,
                label="analyst",
            )
        record["feedback"] = feedback
        return feedback
    except Exception as exc:
        record["error"] = str(exc)
        raise
    finally:
        _write(worker.root / "analysis" / f"{uuid4().hex}.json", json.dumps(plain(record), ensure_ascii=False).encode())
