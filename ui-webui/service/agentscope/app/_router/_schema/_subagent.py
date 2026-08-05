# -*- coding: utf-8 -*-
"""Request / response schemas for the sub-agent router."""
from datetime import datetime

from pydantic import BaseModel, Field


class CreateSubAgentRequest(BaseModel):
    """Request body for creating a sub-agent config."""

    data: dict = Field(description="Sub-Agent config payload.")


class CreateSubAgentResponse(BaseModel):
    """Response body after creating a sub-agent config."""

    subagent_id: str = Field(
        description="Server-assigned sub-agent identifier.",
    )


class UpdateSubAgentRequest(BaseModel):
    """Request body for updating a sub-agent config."""

    data: dict = Field(description="New sub-agent config payload.")


class SubAgentView(BaseModel):
    """A sub-agent config as returned to the frontend."""

    id: str = Field(description="The sub-agent id.")
    data: dict = Field(description="The sub-agent config payload.")
    created_at: datetime = Field(description="Creation time.")
    updated_at: datetime = Field(description="Last update time.")


class ListSubAgentsResponse(BaseModel):
    """Response body for listing sub-agent configs."""

    subagents: list[SubAgentView] = Field(
        description="Sub-Agent config records.",
    )
    total: int = Field(description="Total number of sub-agents.")


class ListSubAgentSchemasResponse(BaseModel):
    """Response body for listing sub-agent type schemas."""

    schemas: list[dict] = Field(
        description="JSON schemas for all registered sub-agent types.",
    )


class SubAgentPreset(BaseModel):
    """A built-in sub-agent preset returned to the frontend."""

    preset_id: str = Field(description="Stable preset identifier.")
    label: str = Field(description="Human-readable preset name.")
    data: dict = Field(description="Ready-to-create config payload.")


class ListSubAgentPresetsResponse(BaseModel):
    """Response body for listing built-in sub-agent presets."""

    presets: list[SubAgentPreset] = Field(
        description="Built-in sub-agent presets.",
    )
