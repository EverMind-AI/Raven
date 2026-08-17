"""Skill Hub client — HTTP access to the remote skill marketplace.

The Hub speaks the ``/openapi/v1/skills`` OpenAPI surface (search /
metadata+body / zip download) behind a uniform envelope
(``{error, requestId, status, result}``). :class:`SkillHubClient` is the
single place that talks to it; it is shared by :class:`HubSkillSource`
(discovery) and the ``read_skill`` / ``use_skill`` tools (body / bundle).
"""

from raven.skill_hub.client import SkillHubClient

__all__ = ["SkillHubClient"]
