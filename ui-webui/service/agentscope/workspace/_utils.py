# -*- coding: utf-8 -*-
"""Host-side helpers shared by workspace implementations.

Constants for the standard workspace layout.

This module is internal to ``agentscope.workspace``. Public-sounding
constants are shared within the package, not exported as user-facing
API.
"""

# ── shared constants ───────────────────────────────────────────────

#: Standard workspace-relative directory for offloaded multimodal data.
DEFAULT_DATA_DIR = "data"

#: Standard workspace-relative directory for reusable skills.
DEFAULT_SKILLS_DIR = "skills"

#: Standard workspace-relative directory for session context and results.
DEFAULT_SESSIONS_DIR = "sessions"

#: Standard workspace-relative file for persisted MCP registrations.
DEFAULT_MCP_FILE = ".mcp"
