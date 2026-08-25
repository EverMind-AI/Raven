# -*- coding: utf-8 -*-
"""The sub-agent instance record (per-session runtime instance)."""

from ._base import _RecordBase


class SubAgentInstanceRecord(_RecordBase):
    """A stateful sub-agent instance materialized within a session.

    Maps an agent-chosen ``handle`` to the CLI session id (``agent_id``)
    that RavenX generated when the instance was created, so later calls
    can RESUME the same CLI conversation.
    """

    session_id: str
    """The RavenX chat session this instance belongs to."""

    handle: str
    """The agent-chosen instance handle, unique within the session."""

    agent_id: str
    """The uuid used as the CLI session id (``{agent_id}``)."""

    prototype_name: str
    """The ``CliSubAgentConfig.name`` this instance is an instance of."""
