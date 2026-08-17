# -*- coding: utf-8 -*-
"""The sub-agent module."""

from ._agent_tools import make_subagent_tool_factory
from ._base import (
    CliSubAgentConfig,
    OpenAISubAgentConfig,
    SubAgentConfigBase,
)
from ._dag import DagValidationError, SubAgentDagTool
from ._factory import SubAgentFactory
from ._instance_registry import SessionInstanceRegistry
from ._openai_tool import OpenAISubAgentTool
from ._presets import list_subagent_presets
from ._tool import CliSubAgentTool

__all__ = [
    "SubAgentConfigBase",
    "CliSubAgentConfig",
    "OpenAISubAgentConfig",
    "SubAgentFactory",
    "CliSubAgentTool",
    "OpenAISubAgentTool",
    "make_subagent_tool_factory",
    "SessionInstanceRegistry",
    "SubAgentDagTool",
    "DagValidationError",
    "list_subagent_presets",
]
