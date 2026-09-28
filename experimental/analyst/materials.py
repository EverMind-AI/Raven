"""Compose what the analyst reads: signals, conversations, record summaries and prior expectations."""

import json
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from ..curator.generation.context.render import tool
from ..curator.harness.declaration import schema_for
from ..curator.raven_adapter.observe import plain
from .activity import activity

RECORDS = "read_records"


def summary(exchange) -> dict:
    execution = exchange.execution
    return {
        "turn_id": execution.turn_id,
        "artifact_id": execution.artifact_id,
        "user": exchange.user,
        "assistant": exchange.assistant,
        "record_kinds": dict(Counter(row["kind"] for row in execution.records)),
        "mechanisms": [
            {key: row[key] for key in ("scope", "target", "decision", "count", "acted", "reasons")}
            for row in activity({"turn": [exchange]})
        ],
        "errors": [row.get("error", row["kind"]) for row in execution.errors],
        "children": [
            {
                "harness": row["harness"],
                "revision": row["revision"],
                "instances": row["instances"],
                "record_kinds": dict(Counter(item["kind"] for item in row["records"])),
            }
            for row in execution.records
            if row["kind"] == "child.execution"
        ],
    }


def materials(
    task, signals, sessions, *, previous_plan, previous_signals, previous_feedback, skills, history=()
) -> dict:
    return {
        "task": task,
        "signals": plain(signals),
        "previous_signals": plain(previous_signals),
        "sessions": {name: [summary(exchange) for exchange in exchanges] for name, exchanges in sessions.items()},
        "previous_expectations": [change.model_dump() for change in previous_plan.changes] if previous_plan else None,
        "previous_feedback": previous_feedback.model_dump(mode="json") if previous_feedback else None,
        "history": list(history),
        "skills": skills,
    }


class RecordQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: str
    kind: str | None = Field(default=None, min_length=1)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=12000, ge=1, le=24000)


def record_tool(sessions) -> dict:
    schema = schema_for(RecordQuery)
    schema["properties"]["turn_id"]["enum"] = [
        exchange.execution.turn_id for exchanges in sessions.values() for exchange in exchanges
    ]
    return tool(
        RECORDS, "Read a turn's execution records, optionally only those whose kind starts with a prefix.", schema
    )


def read_records(sessions, arguments: dict) -> dict:
    request = RecordQuery.model_validate(arguments)
    records = next(
        (
            exchange.execution.records
            for exchanges in sessions.values()
            for exchange in exchanges
            if exchange.execution.turn_id == request.turn_id
        ),
        None,
    )
    if records is None:
        raise ValueError(f"no turn in this round has the id {request.turn_id!r}")
    if request.kind:
        records = [row for row in records if row["kind"].startswith(request.kind)]
    text = json.dumps(records, ensure_ascii=False, indent=2)
    if request.offset > len(text):
        raise ValueError("record offset exceeds the available material")
    end = min(len(text), request.offset + request.length)
    return {
        "turn_id": request.turn_id,
        "text": text[request.offset : end],
        "total_characters": len(text),
        "next_offset": end if end < len(text) else None,
    }
