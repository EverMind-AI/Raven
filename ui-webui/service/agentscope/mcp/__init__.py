# -*- coding: utf-8 -*-
"""The MCP module in AgentScope, that provides fine-grained control over
the MCP servers."""

from ._config import HttpMCPConfig, StdioMCPConfig
from ._mcp_client import MCPClient

__all__ = [
    "MCPClient",
    "StdioMCPConfig",
    "HttpMCPConfig",
]
