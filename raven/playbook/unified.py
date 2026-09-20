"""The durable Playbook v2 contract: optional Harness plus optional Workflow.

Version one files remain the workflow-only ``PlaybookSpec`` contract. Version
two is deliberately separate: the discriminator stays unambiguous on disk and
old readers fail loudly instead of treating a new artifact as a partial v1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from raven.agent.subagent.dag_graph import DagNodeSpec
from raven.config.schema import MCPServerConfig
from raven.playbook.agent_spec import AgentPlaybookSpec
from raven.playbook.types import CamelBase, ParamSpec, PlaybookSpec, Triggers

UNIFIED_SPEC_VERSION = 2


class PlaybookMatch(CamelBase):
    """The portable retrieval description; ranking state stays local."""

    summary: str = Field(min_length=1, max_length=500)
    keywords: list[str] = Field(min_length=1)

    def triggers(self) -> Triggers:
        return Triggers(keywords=self.keywords)


class InputSchema(CamelBase):
    """Runtime values accepted by a reusable workflow."""

    type: Literal["object"] = "object"
    properties: dict[str, ParamSpec] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    additional_properties: bool = False

    @model_validator(mode="after")
    def _required_are_declared(self) -> "InputSchema":
        missing = sorted(set(self.required) - set(self.properties))
        if missing:
            raise ValueError(f"required input(s) are not declared in properties: {', '.join(missing)}")
        for name, param in self.properties.items():
            if param.required != (name in self.required):
                raise ValueError(f"input {name!r}: ParamSpec.required and inputSchema.required disagree")
        return self


class WorkflowSpec(CamelBase):
    """A validated, replayable DAG. No prompt-mode branch exists in v2."""

    summary: str = Field(min_length=1)
    confirm: bool = True
    nodes: list[DagNodeSpec] = Field(min_length=1)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class PlaybookMetadata(CamelBase):
    source_run_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class UnifiedPlaybookSpec(CamelBase):
    """One reusable artifact with two independent, optional dimensions."""

    schema_version: Literal[2] = UNIFIED_SPEC_VERSION
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1, max_length=200)
    state: Literal["draft", "ready"] = "ready"
    match: PlaybookMatch
    input_schema: InputSchema = Field(default_factory=InputSchema)
    harness: AgentPlaybookSpec | None = None
    workflow: WorkflowSpec | None = None
    metadata: PlaybookMetadata = Field(default_factory=PlaybookMetadata)

    @model_validator(mode="after")
    def _has_an_artifact(self) -> "UnifiedPlaybookSpec":
        if self.harness is None and self.workflow is None:
            raise ValueError("a playbook must contain a harness, a workflow, or both")
        if self.harness is not None and not self.harness.delegate:
            raise ValueError("a durable harness must contain at least one worker")
        return self

    @property
    def triggers(self) -> Triggers:
        return self.match.triggers()

    @property
    def params(self) -> dict[str, ParamSpec]:
        return self.input_schema.properties

    # Compatibility projection for the CLI, RPC layer and MCP helpers. These
    # readers predate the unified envelope but consume only workflow fields;
    # keeping that narrow face here lets old and new artifacts share those
    # proven paths without teaching every helper about both disk contracts.
    @property
    def version(self) -> int:
        return self.schema_version

    @property
    def mode(self) -> Literal["dag"]:
        return "dag"

    @property
    def task_summary(self) -> str:
        return self.workflow.summary if self.workflow else self.description

    @property
    def confirm(self) -> bool:
        return self.workflow.confirm if self.workflow else False

    @property
    def nodes(self) -> list[DagNodeSpec] | None:
        return self.workflow.nodes if self.workflow else None

    @property
    def prompts(self) -> None:
        return None

    @property
    def mcp_servers(self) -> dict[str, MCPServerConfig]:
        return self.workflow.mcp_servers if self.workflow else {}

    def as_legacy_workflow(self) -> PlaybookSpec | None:
        """Project the v2 Workflow onto the proven v1 DAG executor."""
        if self.workflow is None:
            return None
        return PlaybookSpec(
            name=self.name,
            description=self.description,
            taskSummary=self.workflow.summary,
            version=1,
            mode="dag",
            confirm=self.workflow.confirm,
            triggers=self.triggers,
            params=self.params,
            mcpServers=self.workflow.mcp_servers,
            nodes=self.workflow.nodes,
        )

    def block_dump(self) -> dict[str, Any]:
        data = self.model_dump(by_alias=True, exclude_none=True)
        data.pop("name", None)
        data.pop("description", None)
        return data


StoredPlaybook = PlaybookSpec | UnifiedPlaybookSpec


def is_unified_data(data: dict[str, Any]) -> bool:
    return data.get("schemaVersion") == UNIFIED_SPEC_VERSION


def unified_from_legacy(spec: PlaybookSpec, *, name: str | None = None) -> UnifiedPlaybookSpec:
    """Wrap one generated legacy DAG in the durable unified contract."""
    if spec.mode != "dag" or not spec.nodes:
        raise ValueError("a unified Workflow must be generated as a concrete DAG")
    required = [key for key, value in spec.params.items() if value.required]
    return UnifiedPlaybookSpec(
        name=name or spec.name,
        description=spec.description,
        match=PlaybookMatch(summary=spec.description, keywords=spec.triggers.keywords),
        inputSchema=InputSchema(properties=spec.params, required=required),
        workflow=WorkflowSpec(
            summary=spec.task_summary,
            confirm=spec.confirm,
            nodes=spec.nodes,
            mcpServers=spec.mcp_servers,
        ),
    )


__all__ = [
    "InputSchema",
    "PlaybookMatch",
    "PlaybookMetadata",
    "StoredPlaybook",
    "UNIFIED_SPEC_VERSION",
    "UnifiedPlaybookSpec",
    "WorkflowSpec",
    "is_unified_data",
    "unified_from_legacy",
]
