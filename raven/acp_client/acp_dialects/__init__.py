"""Picking the reader for one ACP adapter's tool-call updates.

Selected from ``agentInfo.name`` in the connection's own ``initialize`` result,
not from config: the name is what the process serving the requests reports about
itself, so an agent the operator renamed, or two entries pointing at the same
adapter, both resolve correctly and nothing has to be declared.

An adapter with no dialect of its own gets :class:`AcpDialect`, which is the
spec. That is the honest default -- every field it reads is one the protocol
requires -- and it is what keeps an adapter nobody has measured from needing a
file here before it works at all.
"""

from __future__ import annotations

from typing import Any

from raven.acp_client.acp_dialects.base import AcpDialect, DialectResult, ToolCall, content_texts
from raven.acp_client.acp_dialects.claude_code import ClaudeCodeDialect
from raven.acp_client.acp_dialects.codex import CodexDialect

_DIALECTS: tuple[AcpDialect, ...] = (ClaudeCodeDialect(), CodexDialect())

_SPEC = AcpDialect()


def dialect_for(initialize: Any) -> AcpDialect:
    """The dialect for the agent that answered this ``initialize``."""
    name = _agent_info(initialize).get("name")
    return _for_name(name if isinstance(name, str) else "")


def agent_version(initialize: Any) -> str | None:
    """The version the agent that answered this ``initialize`` reports, if it reports one."""
    version = _agent_info(initialize).get("version")
    return version if isinstance(version, str) else None


def _agent_info(initialize: Any) -> dict[str, Any]:
    info = initialize.get("agentInfo") if isinstance(initialize, dict) else None
    return info if isinstance(info, dict) else {}


def _for_name(agent_name: str) -> AcpDialect:
    """The dialect whose key appears in ``agent_name``, else the spec."""
    lowered = agent_name.lower()
    for dialect in _DIALECTS:
        if dialect.key in lowered:
            return dialect
    return _SPEC


__all__ = [
    "AcpDialect",
    "ClaudeCodeDialect",
    "CodexDialect",
    "ToolCall",
    "DialectResult",
    "agent_version",
    "content_texts",
    "dialect_for",
]
