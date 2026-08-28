# -*- coding: utf-8 -*-
"""Schema models for the agent service."""

from ._agent import (
    AgentSchemaResponse,
    AgentSchemaV2Response,
    CreateAgentRequest,
    CreateAgentResponse,
    ListAgentsResponse,
    UpdateAgentRequest,
)
from ._chat import ChatRequest, ChatTriggerResponse
from ._credential import (
    CreateCredentialRequest,
    CreateCredentialResponse,
    ListCredentialSchemasResponse,
    ListCredentialsResponse,
    ModelCardConfig,
    UpdateCredentialModelsRequest,
    UpdateCredentialRequest,
)
from ._knowledge_base import (
    CreateKnowledgeBaseRequest,
    CreateKnowledgeBaseResponse,
    KbEmbeddingProvider,
    KbMiddlewareParametersSchemaResponse,
    KnowledgeDocumentView,
    ListKbEmbeddingModelsResponse,
    ListKnowledgeBasesResponse,
    ListKnowledgeDocumentsResponse,
    ListKnowledgeDocumentStatusResponse,
    ListSupportedContentTypesResponse,
    SearchKnowledgeBaseRequest,
    SearchKnowledgeBaseResponse,
    UpdateKnowledgeBaseRequest,
    UploadKnowledgeDocumentResponse,
)
from ._model import ListModelsRequest, ListModelsResponse
from ._schedule import (
    CreateScheduleRequest,
    CreateScheduleResponse,
    ListSchedulesResponse,
    ScheduleSessionsResponse,
    UpdateScheduleRequest,
)
from ._session import (
    CreateSessionRequest,
    CreateSessionResponse,
    InterruptSessionResponse,
    ListMessagesResponse,
    ListSessionsResponse,
    SessionStatus,
    SessionStatusResponse,
    SessionView,
    TeamDetailResponse,
    TeamMemberView,
    UpdateSessionRequest,
)
from ._subagent import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    ListSubAgentPresetsResponse,
    ListSubAgentSchemasResponse,
    ListSubAgentsResponse,
    SubAgentPreset,
    SubAgentView,
    UpdateSubAgentRequest,
)
from ._tts_model import ListTTSModelsRequest, ListTTSModelsResponse

__all__ = [
    # Agent
    "AgentSchemaResponse",
    "AgentSchemaV2Response",
    "ListAgentsResponse",
    "CreateAgentRequest",
    "CreateAgentResponse",
    "UpdateAgentRequest",
    "ListSchedulesResponse",
    # Chat
    "ChatRequest",
    "ChatTriggerResponse",
    # Credential
    "CreateCredentialRequest",
    "CreateCredentialResponse",
    "UpdateCredentialRequest",
    "ListCredentialsResponse",
    "ListCredentialSchemasResponse",
    "ModelCardConfig",
    "UpdateCredentialModelsRequest",
    # Knowledge base
    "CreateKnowledgeBaseRequest",
    "CreateKnowledgeBaseResponse",
    "KbEmbeddingProvider",
    "KbMiddlewareParametersSchemaResponse",
    "KnowledgeDocumentView",
    "ListKbEmbeddingModelsResponse",
    "ListKnowledgeBasesResponse",
    "ListKnowledgeDocumentsResponse",
    "ListKnowledgeDocumentStatusResponse",
    "ListSupportedContentTypesResponse",
    "SearchKnowledgeBaseRequest",
    "SearchKnowledgeBaseResponse",
    "UpdateKnowledgeBaseRequest",
    "UploadKnowledgeDocumentResponse",
    # Model
    "ListModelsRequest",
    "ListModelsResponse",
    # TTS Model
    "ListTTSModelsRequest",
    "ListTTSModelsResponse",
    # Schedule
    "CreateScheduleRequest",
    "CreateScheduleResponse",
    "ListSchedulesResponse",
    "ScheduleSessionsResponse",
    "UpdateScheduleRequest",
    # Session
    "CreateSessionRequest",
    "CreateSessionResponse",
    "InterruptSessionResponse",
    "UpdateSessionRequest",
    "ListSessionsResponse",
    "ListMessagesResponse",
    "SessionStatus",
    "SessionStatusResponse",
    "SessionView",
    "TeamDetailResponse",
    "TeamMemberView",
    # Sub-Agent
    "CreateSubAgentRequest",
    "CreateSubAgentResponse",
    "UpdateSubAgentRequest",
    "SubAgentView",
    "ListSubAgentsResponse",
    "ListSubAgentSchemasResponse",
    "ListSubAgentPresetsResponse",
    "SubAgentPreset",
]
