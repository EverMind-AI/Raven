# -*- coding: utf-8 -*-
"""The task tool base class, providing unified interface and permission
check for builtin task related tools."""
from typing import Any

from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from .._base import ToolBase


class _TaskToolBase(ToolBase):
    name: str

    description: str

    input_schema: dict

    is_concurrency_safe: bool = True

    is_read_only: bool = False

    is_state_injected: bool = True

    is_external_tool: bool = False

    is_mcp: bool = False

    mcp_name: str | None = None

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Check permission for the tool usage."""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed to be called.",
        )
