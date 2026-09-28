"""Supply the task and a compact account of the actual worker for understanding."""

from pydantic import BaseModel, ConfigDict, Field

from ...harness.channels import execution_control, model_decision, model_input, tool_interaction
from ...harness.declaration import schema_for
from ...harness.view import Mechanism, project
from ..context import render
from ..context.collect import Context, read_reference
from ..context.render import tool


def materials(context: Context) -> dict:
    facts = context.facts
    worker = {
        key: facts[key]
        for key in (
            "backend",
            "hosting",
            "allow_delegation",
            "origin",
            "resident",
            "native_modules",
            "mode",
            "turn_profile",
            "mechanisms",
            "hooks",
            "memory_backend",
            "context_engine",
            "unavailable",
            "loop_contract",
            "task",
            "scope",
            "composition",
            "assigned_nodes",
            "prompts",
        )
        if key in facts
    }
    mechanisms = tuple(Mechanism.model_validate(item) for item in facts.get("mechanisms", ()))
    worker["responsibilities"] = {
        role: [item.name for item in project(mechanisms, role=role)]
        for role in ("memory", "planning", "capability", "action")
    }
    worker["channels"] = {
        channel.NAME: [item.name for item in channel.current(mechanisms)]
        for channel in (model_input, model_decision, tool_interaction, execution_control)
    }
    for role in worker["responsibilities"]:
        if role in facts:
            worker[role] = facts[role]
    worker["tools"] = [
        {"name": item.get("function", {}).get("name"), "description": item.get("function", {}).get("description")}
        for item in facts.get("tools", ())
    ]
    worker["plugins"] = [
        {
            "id": plugin["id"],
            "contributions": {
                kind: [entry["name"] for entry in entries]
                for kind, entries in plugin.get("contributes", {}).items()
                if entries
            },
        }
        for plugin in facts.get("plugins", [])
    ]
    worker["skills"] = facts.get("skills", [])
    worker["bootstrap_paths"] = list(facts.get("bootstrap", {}))
    worker["authored_targets"] = list(facts.get("authored", {}).get("values", {}))
    # Ordered from what changes least to what changes most (the exploration paths are new every curation), so
    # requests of later rounds keep the longest possible prefix for the provider's cache.
    return {
        "task": context.task,
        "orientation": read_reference(context, "reference.index"),
        "fact_sections": list(facts),
        "worker": worker,
        "current_authored": {
            "values": facts.get("authored", {}).get("values", {}),
            "files": facts.get("authored", {}).get("files", {}),
        },
        "previous_expectations": context.previous_plan.model_dump(mode="json") if context.previous_plan else None,
        "feedback": context.feedback,
        "observations": render.observation_index(context.observations),
        "sources": {
            name: {
                "lines": entry["end"] - entry["start"] + 1,
                "path": entry.get("snapshot_path"),
            }
            for name, entry in context.sources.items()
        },
        "exploration": context.exploration,
    }


GAP = "report_gap"


class Gap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)


def gap_tool() -> dict:
    return tool(
        GAP, "Report missing authority, capability or information instead of inventing a solution.", schema_for(Gap)
    )


def parse_gap(arguments: dict) -> str:
    return Gap.model_validate(arguments).reason
