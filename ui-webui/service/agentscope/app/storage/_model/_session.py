# -*- coding: utf-8 -*-
"""The session data class for storage."""
import os
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from ....state import AgentState
from ._base import _RecordBase


class SessionSource(str, Enum):
    """The source that created the session."""

    USER = "user"
    SCHEDULE = "schedule"


class ChatModelConfig(BaseModel):
    """The model configuration class."""

    type: str
    """The provider type."""

    credential_id: str
    """The credential id."""

    model: str
    """The model name."""

    parameters: dict
    """The model parameters."""


class TTSModelConfig(BaseModel):
    """The TTS model configuration class."""

    type: str
    """The provider type."""

    credential_id: str
    """The credential id."""

    model: str
    """The TTS model name."""

    parameters: dict
    """TTS parameters (voice, language, etc.)."""


class EmbeddingModelConfig(BaseModel):
    """Configuration for constructing an embedding model from a credential.

    Mirrors :class:`ChatModelConfig` but targets
    :class:`~agentscope.embedding.EmbeddingModelBase` subclasses.
    Used by :class:`KnowledgeBaseRecord` to persist the user's
    embedding model selection.
    """

    type: str
    """The provider type (e.g. ``"openai_credential"``)."""

    credential_id: str
    """The credential id to use for authentication."""

    model: str
    """The embedding model name (e.g. ``"text-embedding-3-small"``)."""

    dimensions: int = Field(..., gt=0)
    """The output embedding vector dimensions.

    Required and first-class — chosen at config-creation time and
    pinned to the resulting :class:`KnowledgeBaseRecord` so subsequent
    indexing / retrieval calls are dim-deterministic without any
    fallback lookup.
    """

    parameters: dict = Field(default_factory=dict)
    """The provider-specific non-dimensional parameters.

    Does **not** carry ``dimensions`` — that field is promoted to a
    top-level attribute above.
    """


class SessionKnowledgeConfig(BaseModel):
    """Session-level knowledge base attachment.

    Persists which knowledge bases the agent should retrieve from for
    this session and how the
    :class:`~agentscope.middleware.RAGMiddleware` should be
    configured.  ``parameters`` carries the user-tunable middleware
    fields verbatim (mirrors :attr:`ChatModelConfig.parameters`); the
    accepted keys and value types are described by
    :meth:`RAGMiddleware.Config.model_json_schema`.
    """

    knowledge_base_ids: list[str] = Field(default_factory=list)
    """Ids of the knowledge bases attached to this session.

    Empty list means no knowledge base is wired and the middleware is
    not installed.
    """

    parameters: dict = Field(default_factory=dict)
    """Middleware parameters keyed by ``RAGMiddleware``'s
    :class:`Config` model fields (``mode``, ``top_k``,
    ``score_threshold``, ``emit_hint_event``, ``persist_hint``,
    ``hint_template``).
    """


class SessionConfig(BaseModel):
    """Session configuration — set at creation, updatable via PATCH."""

    workspace_id: str
    """Authoritative workspace binding for the session.

    Populated at session creation — either from an explicit
    ``workspace_id`` on ``CreateSessionRequest`` (used by team
    invite/borrow flows) or from
    :meth:`WorkspaceManagerBase.assign_workspace_id` under the
    manager's isolation policy. Consumed verbatim by chat,
    ``list_mcps``, and team tools; also the cache key for
    :meth:`WorkspaceManagerBase.get_workspace`."""

    name: str = Field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        description="Display name for the session.",
    )
    """The session display name."""

    chat_model_config: ChatModelConfig | None = None
    """The chat model config. None means no model has been configured yet."""

    fallback_chat_model_config: ChatModelConfig | None = None
    """The fallback chat model config. Used as a backup when the primary
    model fails. None means no fallback configured."""

    tts_model_config: TTSModelConfig | None = None
    """The TTS model config. None means TTS is not enabled."""

    knowledge_config: SessionKnowledgeConfig | None = None
    """Knowledge bases attached to this session and the corresponding
    :class:`~agentscope.middleware.RAGMiddleware` parameters.
    ``None`` means no knowledge base is wired."""

    work_dir: str | None = Field(
        default=None,
        description=(
            "Optional working-directory override for the session. When "
            "set it must be an absolute path and becomes the working "
            "directory used by the session's agent tools and CLI "
            "sub-agents (unless a sub-agent pins its own ``cwd``). "
            "``None`` means the session uses its default workspace "
            "directory (``basedir/<workspace_id>``)."
        ),
    )
    """The session working-directory override (absolute path), or None."""

    @field_validator("work_dir")
    @classmethod
    def _require_absolute_work_dir(cls, value: str | None) -> str | None:
        """Ensure an explicit ``work_dir`` is an absolute path.

        Args:
            value (`str | None`):
                The working-directory override to validate.

        Returns:
            `str | None`:
                The validated value (``None`` passes through unchanged).
        """
        if value is not None and not os.path.isabs(value):
            raise ValueError("work_dir must be an absolute path")
        return value


class SessionRecord(_RecordBase):
    """The session record."""

    user_id: str
    """The user id."""

    agent_id: str
    """The agent id."""

    source: SessionSource = SessionSource.USER
    """The source that created this session."""

    source_schedule_id: str | None = None
    """The source schedule Id."""

    team_id: str | None = None
    """The team this session participates in, if any.

    Team membership is session-level: a user agent can lead multiple teams
    across different sessions, and each worker session belongs to exactly
    one team. ``None`` means the session is not part of any team.
    """

    config: SessionConfig
    """Session configuration (workspace, name, model)."""

    state: AgentState = Field(default_factory=AgentState)
    """Mutable runtime state, updated after each chat turn."""
