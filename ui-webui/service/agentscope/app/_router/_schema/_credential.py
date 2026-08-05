# -*- coding: utf-8 -*-
"""Request / response schemas for the credential router."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..._service import CredentialView


class CreateCredentialRequest(BaseModel):
    """Request body for creating a new credential."""

    data: dict = Field(description="Credential payload (e.g. API keys).")


class CreateCredentialResponse(BaseModel):
    """Response body after creating a credential."""

    credential_id: str = Field(
        description="Server-assigned credential identifier.",
    )


class UpdateCredentialRequest(BaseModel):
    """Request body for updating an existing credential."""

    data: dict = Field(description="New credential payload.")


class ListCredentialsResponse(BaseModel):
    """Response body for listing credentials."""

    credentials: list[CredentialView] = Field(
        description="Credential records.",
    )
    total: int = Field(description="Total number of credentials.")


class ListCredentialSchemasResponse(BaseModel):
    """Response body for listing credential type schemas."""

    schemas: list[dict] = Field(
        description="JSON schemas for all registered credential types.",
    )


class ModelCardConfig(BaseModel):
    """One editable model-card config (YAML-equivalent)."""

    name: str = Field(description="The model id, e.g. ``deepseek-v4-flash``.")
    label: str | None = Field(
        default=None,
        description="Display label. Defaults to ``name`` when omitted.",
    )
    status: Literal["active", "deprecated", "sunset"] = Field(
        default="active",
        description="The model status.",
    )
    deprecated_at: datetime | None = Field(
        default=None,
        description="The deprecation date, if any.",
    )
    input_types: list[str] = Field(
        default=["text/plain"],
        description="Supported input MIME types.",
    )
    output_types: list[str] = Field(
        default=["text/plain"],
        description="Supported output MIME types.",
    )
    context_size: int = Field(gt=0, description="The context window size.")
    output_size: int = Field(gt=0, description="Max output tokens.")
    parameter_overrides: dict = Field(
        default_factory=dict,
        description="Advanced per-parameter schema overrides.",
    )


class UpdateCredentialModelsRequest(BaseModel):
    """Replace a credential's model override list.

    ``None`` reverts to the built-in static catalog; an empty list means
    the credential offers no models.
    """

    models: list[ModelCardConfig] | None = Field(
        default=None,
        description="The new model override list, or ``None`` to inherit.",
    )
