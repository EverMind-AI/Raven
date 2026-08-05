# -*- coding: utf-8 -*-
"""The workspace module in agentscope."""


from ._base import WorkspaceBase
from ._local_workspace import LocalWorkspace
from ._offload_protocol import Offloader

__all__ = [
    "WorkspaceBase",
    "LocalWorkspace",
    "Offloader",
]
