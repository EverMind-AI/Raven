# -*- coding: utf-8 -*-
"""The workspace manager classes, responsible for managing the resources
and their lifecycles, and filesystem isolation."""

from ._base import IsolationPolicy, WorkspaceManagerBase
from ._local_workspace_manager import LocalWorkspaceManager

__all__ = [
    "IsolationPolicy",
    "WorkspaceManagerBase",
    "LocalWorkspaceManager",
]
