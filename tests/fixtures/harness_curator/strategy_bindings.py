"""Host translations for the independent strategy examples."""

from experimental.curator.raven_adapter.targets.action import ReviewResult

from .task_action import Decision, Failure, Proposal
from .task_capability import Need, Selection
from .task_memory import Context, Evidence, Query


def evidence(step):
    for row in reversed(step.transcript[step.turn_base :]):
        if row.get("role") == "tool" and row.get("name") == "evidence_probe":
            lines = [line for line in row.get("content", "").splitlines() if line.startswith("FACT:")]
            if len(lines) == 1:
                return Evidence(call_id=row["tool_call_id"], value=lines[0].removeprefix("FACT:"))
    return None


def query(step) -> Query:
    return Query(text="")


def context(value: Context) -> str:
    return "MEMORY_CONTEXT:" + ",".join(value.facts)


def retain(step) -> Evidence | None:
    return evidence(step)


def need(offered, step) -> Need:
    return Need(offered=[row["function"]["name"] for row in offered])


def expose(selection: Selection) -> list[str]:
    return selection.names


def proposal(step) -> Proposal:
    return Proposal(
        has_evidence=evidence(step) is not None, pending_tools=bool(getattr(step.response, "tool_calls", None))
    )


def decision(value: Decision) -> ReviewResult:
    if value.kind == "retry":
        return ReviewResult(
            verdict="resample", reason="No execution evidence", inject=[{"role": "user", "content": value.text}]
        )
    if value.kind == "finish":
        return ReviewResult(verdict="end", reply=value.text)
    return ReviewResult(verdict="accept")


def failure(step) -> Failure:
    return Failure(question=step.question)


def reply(value: Decision) -> str:
    if value.kind != "finish":
        raise ValueError("terminal salvage cannot execute this decision")
    return value.text


def capability_context(selection: Selection) -> str:
    return "CAPABILITY_USAGE:" + ",".join(selection.names)
