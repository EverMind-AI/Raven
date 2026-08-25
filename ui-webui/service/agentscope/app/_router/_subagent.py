# -*- coding: utf-8 -*-
"""Sub-Agent router — CRUD endpoints for CLI sub-agent configs."""
from fastapi import APIRouter, Depends, HTTPException, status

from ...subagent import SubAgentFactory, list_subagent_presets
from ..deps import get_current_user_id, get_storage
from ..storage import StorageBase
from ._schema import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    ListSubAgentPresetsResponse,
    ListSubAgentSchemasResponse,
    ListSubAgentsResponse,
    SubAgentPreset,
    SubAgentView,
    UpdateSubAgentRequest,
)

subagent_router = APIRouter(
    prefix="/subagent",
    tags=["subagent"],
    responses={404: {"description": "Not found"}},
)


@subagent_router.get(
    "/schemas",
    response_model=ListSubAgentSchemasResponse,
    summary="List JSON schemas for all sub-agent types",
)
async def list_subagent_schemas() -> ListSubAgentSchemasResponse:
    """Return JSON schemas for all registered sub-agent types."""
    return ListSubAgentSchemasResponse(
        schemas=SubAgentFactory.list_schemas(),
    )


@subagent_router.get(
    "/presets",
    response_model=ListSubAgentPresetsResponse,
    summary="List built-in sub-agent presets",
)
async def list_presets() -> ListSubAgentPresetsResponse:
    """Return the built-in CLI sub-agent presets.

    Returns:
        `ListSubAgentPresetsResponse`: The built-in presets.
    """
    return ListSubAgentPresetsResponse(
        presets=[
            SubAgentPreset(**preset) for preset in list_subagent_presets()
        ],
    )


@subagent_router.get(
    "/",
    response_model=ListSubAgentsResponse,
    summary="List all sub-agents",
)
async def list_subagents(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListSubAgentsResponse:
    """Return all sub-agent configs owned by the authenticated user.

    Args:
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `ListSubAgentsResponse`: All sub-agent configs for the user.
    """
    records = await storage.list_subagents(user_id)
    views = [
        SubAgentView(
            id=record.id,
            data=record.data,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        for record in records
    ]
    return ListSubAgentsResponse(subagents=views, total=len(views))


@subagent_router.post(
    "/",
    response_model=CreateSubAgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new sub-agent",
)
async def create_subagent(
    body: CreateSubAgentRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> CreateSubAgentResponse:
    """Store a new sub-agent config.

    Args:
        body (`CreateSubAgentRequest`): The sub-agent payload.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `CreateSubAgentResponse`: The server-assigned sub-agent id.
    """
    try:
        config = SubAgentFactory.from_dict(body.data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    subagent_id = await storage.upsert_subagent(user_id, config)
    return CreateSubAgentResponse(subagent_id=subagent_id)


@subagent_router.patch(
    "/{subagent_id}",
    response_model=SubAgentView,
    summary="Update a sub-agent",
)
async def update_subagent(
    subagent_id: str,
    body: UpdateSubAgentRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> SubAgentView:
    """Replace the payload of an existing sub-agent config.

    Args:
        subagent_id (`str`): The sub-agent to update.
        body (`UpdateSubAgentRequest`): New sub-agent payload.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `SubAgentView`: The updated record.

    Raises:
        `HTTPException`: 404 if the sub-agent does not exist; 422 on an
            invalid payload.
    """
    existing = await storage.get_subagent(user_id, subagent_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sub-Agent {subagent_id!r} not found.",
        )
    try:
        config = SubAgentFactory.from_dict(body.data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    config.id = subagent_id
    await storage.upsert_subagent(user_id, config)
    updated = await storage.get_subagent(user_id, subagent_id)
    if updated is None:
        raise RuntimeError(
            f"Sub-Agent {subagent_id!r} disappeared after upsert.",
        )
    return SubAgentView(
        id=updated.id,
        data=updated.data,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@subagent_router.delete(
    "/{subagent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a sub-agent",
)
async def delete_subagent(
    subagent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """Permanently delete a sub-agent config.

    Args:
        subagent_id (`str`): The sub-agent to delete.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Raises:
        `HTTPException`: 404 if the sub-agent does not exist.
    """
    deleted = await storage.delete_subagent(user_id, subagent_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sub-Agent {subagent_id!r} not found.",
        )
