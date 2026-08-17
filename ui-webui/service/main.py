# -*- coding: utf-8 -*-
"""The example script to start the agent service."""

import os

import raven_gateway_agent
import uvicorn
from agentscope.app import SubAgentTemplate, create_app
from agentscope.app._router import _session as _as_session
from agentscope.app._service import (
    CredentialView,
    ResourceAccessService,
)
from agentscope.app._service import _chat as _as_chat
from agentscope.app.access import ResourceKind
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import (
    IsolationPolicy,
    LocalWorkspaceManager,
)
from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.rag import QdrantStore
from agentscope.subagent import make_subagent_tool_factory
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from raven_config_routes import build_raven_config_router

default_mcps = [
    MCPClient(
        name="browser-use",
        mcp_config=StdioMCPConfig(
            command="npx",
            args=["@playwright/mcp@latest"],
        ),
        is_stateful=True,
    ),
]

if os.getenv("AMAP_API_KEY"):
    default_mcps.append(
        MCPClient(
            name="amap",
            mcp_config=HttpMCPConfig(
                url=f"https://mcp.amap.com/mcp?key={os.environ['AMAP_API_KEY']}",
            ),
            is_stateful=False,
        ),
    )

storage = RedisStorage(
    host="localhost",
    port=6379,
)

# On disk rather than ":memory:": an in-process store drops every indexed
# document on restart while the knowledge base records survive in Redis, so the
# page keeps reporting documents as "ready" and every search returns nothing.
# That reads as broken retrieval rather than an empty index. A local-path Qdrant
# locks the directory to one process, which matches this single-service
# deployment.
vector_store = QdrantStore(
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "qdrant"),
)

workspace_manager = LocalWorkspaceManager(
    basedir=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "workspaces",
    ),
    isolation=IsolationPolicy.PER_SESSION,
    # The default MCP servers that will be added into the workspace
    default_mcps=default_mcps,
)

message_bus = InMemoryMessageBus()


# The chat's main turn always runs on RavenGatewayAgent: it connects to a
# persistent `raven gateway` web channel over WebSocket and translates its spine
# events into AgentScope events (P2). Requires a running `raven gateway` with
# gateway.web.enabled=true (see RAVEN_GATEWAY_WS_URL).
raven_gateway_agent.MESSAGE_BUS = message_bus
raven_gateway_agent.STORAGE = storage
_agent_cls = raven_gateway_agent.RavenGatewayAgent

# The AgentScope model object is never called -- the real model is the session's,
# resolved inside raven. Replace the resolver so a chat turn does not require a
# record in AgentScope's credential store, which the raven-native Credentials
# page no longer writes.
if not hasattr(_as_chat, "get_model"):
    raise RuntimeError(
        "agentscope.app._service._chat.get_model is gone; the gateway model "
        "shim needs updating before the service can start"
    )


class _RavenModelStub(ChatModelBase):
    """Duck-types the attribute AgentScope reads off a chat model, and
    subclasses ChatModelBase (rather than a plain object) because
    ModelConfig.fallback_model is a pydantic field that isinstance-checks
    against ChatModelBase -- a bare duck-typed object fails that check."""

    def __init__(self, name: str) -> None:
        self.model = name

    async def _call_api(self, *args, **kwargs):
        raise NotImplementedError(
            "the raven gateway model stub is never called; the turn runs on raven's model instead"
        )


async def _resolve_raven_model(user_id, config, access):
    return _RavenModelStub(getattr(config, "model", "") or "")


_as_chat.get_model = _resolve_raven_model

# Why this shim exists: the chat page must be able to write a session's
# placeholder chat_model_config (_chat.py raises HTTP 404 on a null config
# before the shimmed get_model above is ever reached), and this check would
# reject it -- the credential it names has no record, because the
# raven-native Credentials page no longer writes AgentScope's store. It
# gates only updates that carry a model config: the check returns
# immediately when its config argument is None, and the frontend PATCHes
# partial bodies, so an update touching anything else was never affected.
#
# For chat_model_config, gateway mode never instantiates AgentScope's
# model objects (see above), so this check has nothing to validate.
# It also covers fallback_chat_model_config and tts_model_config, which
# is safe even though tts_model_config is NOT inert -- get_tts_model
# (unshimmed) still resolves and uses that credential. What makes
# disabling this specific check safe either way is that it is redundant
# defense-in-depth, not the authorization boundary: for
# ResourceKind.CREDENTIAL, the check being disabled
# (ResourceAccessService.get_resource) and the one that survives
# (resolve_credential) evaluate the identical predicate over the same
# owner-scoped storage -- not just "both strict", the same test.
# Removing this check only changes WHEN an inaccessible credential is
# caught, not WHETHER access is enforced -- except tts_model_config
# now trades a rejected write for a credential that silently fails
# inside the chat turn: ChatService.run swallows the exception and
# only logs it, so no error reaches the client. Self-inflicted by the
# user's own input, not cross-user exposure.
if not hasattr(_as_session, "_ensure_credential_exists"):
    raise RuntimeError(
        "agentscope.app._router._session._ensure_credential_exists is "
        "gone; the gateway credential-check shim needs updating before "
        "the service can start"
    )


async def _skip_credential_check(access, user_id, config) -> None:
    return None


_as_session._ensure_credential_exists = _skip_credential_check

# POST /schedule/ calls access.get_resource directly instead of going
# through the shim above, so schedule creation 404s on the placeholder
# credential_id the picker emits (RAVEN_CREDENTIAL_SENTINEL below mirrors
# AGENTSCOPE_MODEL_PLACEHOLDER.credential_id in ChatViewport.tsx and the
# value LlmSelect.handleSelect puts in every config it emits).
#
# Why a one-id whitelist rather than a third no-op: get_resource IS the
# deny path for real credential ids -- a no-op would let any viewer resolve
# another owner's credential, which the two shims above cannot do (they
# only drop a redundant pre-check whose surviving twin, resolve_credential,
# evaluates the same predicate). Only the sentinel is short-circuited, to a
# view holding no secret material; every other id, real-but-not-yours
# included, still takes the unmodified owned/shared/404 path.
if not hasattr(ResourceAccessService, "get_resource"):
    raise RuntimeError(
        "agentscope.app._service._access.ResourceAccessService.get_resource "
        "is gone; the gateway credential-sentinel shim needs updating "
        "before the service can start"
    )

RAVEN_CREDENTIAL_SENTINEL = "raven"
_get_resource_unshimmed = ResourceAccessService.get_resource


async def _get_resource_or_raven_sentinel(self, viewer_id, kind, resource_id):
    if kind is ResourceKind.CREDENTIAL and resource_id == RAVEN_CREDENTIAL_SENTINEL:
        return CredentialView(
            id=RAVEN_CREDENTIAL_SENTINEL,
            user_id=viewer_id,
            data={
                "type": "raven_placeholder",
                "name": "placeholder - raven resolves the model, nothing is stored here",
            },
            editable=False,
        )
    return await _get_resource_unshimmed(self, viewer_id, kind, resource_id)


ResourceAccessService.get_resource = _get_resource_or_raven_sentinel

app = create_app(
    storage=storage,
    message_bus=message_bus,
    custom_agent_cls=_agent_cls,
    # -- To use a Redis-backed message bus instead (recommended for
    # -- multi-process / production deployments), uncomment the lines
    # -- below and replace the InMemoryMessageBus() above:
    #
    # from agentscope.app.message_bus import RedisMessageBus
    # message_bus=RedisMessageBus(
    #     host="localhost",
    #     port=6379,
    # ),
    workspace_manager=workspace_manager,
    # Knowledge base feature — backed by an in-memory Qdrant store. The
    # CollectionPerKbManager allocates one collection per knowledge base,
    # so any embedding dimension is allowed.
    knowledge_base_manager=CollectionPerKbManager(
        storage=storage,
        vector_store=vector_store,
    ),
    # Wire the CLI sub-agent tool factory so the leader agent can spawn
    # and manage command-line-invoked domain sub-agents.
    extra_agent_tools=make_subagent_tool_factory(
        storage,
        workspace_manager,
        message_bus,
    ),
    # Customize your own subagent templates
    custom_subagent_templates=[
        SubAgentTemplate(
            type="explorer",
            description=(
                "Read-only agents specialized in exploration tasks. It can "
                "read files but cannot modify, create, or delete them. Use "
                "this agent type when you need to investigate the codebase, "
                "understand its structure, or gather information from files "
                "to support planning—without making any changes."
            ),
            system_prompt_template="""You are {member_name}, an explorer \
agent in team '{team_name}' led by {leader_name}.

Team purpose: {team_description}

Your role: {member_description}

## Responsibilities
- Complete the exploration tasks assigned by the team leader.
- You are read-only: you may inspect files and the codebase, but you must \
never modify, create, or delete anything.

## Reporting
- Always report the task result back to {leader_name} using the TeamSay \
tool, whether the task succeeds or fails.
- Keep your private reasoning private; only share conclusions and findings \
that the leader needs.

Note: `TeamSay` is your ONLY channel to communicate with {leader_name} and \
the other team members. Any other output you produce is invisible to them, \
so anything you want them to see MUST be sent through `TeamSay`.""",
            permission_context=PermissionContext(
                # Read-only
                mode=PermissionMode.EXPLORE,
            ),
        ),
    ],
    extra_middlewares=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ],
)

try:
    from raven_providers_routes import build_raven_providers_router

    app.include_router(build_raven_providers_router())
except ImportError as exc:  # raven not importable in this env — skip the page's routes
    import logging

    logging.getLogger("uvicorn.error").warning("raven not importable; /raven/providers routes disabled: %s", exc)

# Two knowledge-base routes AgentScope's own app does not provide here. The
# embedding picker is served from Raven's config because AgentScope's walks the
# caller's AgentScope credentials and asks each provider for model cards, and
# this deployment has neither -- so the picker was always empty and no knowledge
# base could be created. Preview reads a document's stored bytes back, which the
# document routes cover for upload, list, status and delete but not for reading.
#
# Guarded because neither is load-bearing for the chat: a service that cannot
# install them should still boot with retrieval working.
try:
    from agentscope.app.deps import get_blob_store, get_current_user_id
    from raven_kb_embedding import install_kb_embedding_override
    from raven_kb_preview import build_kb_preview_router

    install_kb_embedding_override(app, storage)
    app.include_router(build_kb_preview_router(storage, get_current_user_id, get_blob_store))
except Exception as exc:  # noqa: BLE001 - the rest of the service must still boot
    import logging

    logging.getLogger("uvicorn.error").warning("knowledge: page helpers not installed: %s", exc)

# The chat runs on the gateway, so also expose the Raven config-admin REST proxy
# (P4) and close the shared gateway WebSocket connection on shutdown.
app.include_router(build_raven_config_router())
# Close the shared gateway WS on shutdown. Use the starlette primitive (FastAPI
# 0.139 dropped add_event_handler); guarded so a create_app that uses a lifespan
# context (where on_shutdown is ignored) still boots.
try:
    app.router.on_shutdown.append(raven_gateway_agent.GatewayClient.aclose_shared)
except Exception:  # noqa: BLE001
    pass


if __name__ == "__main__":
    # Start the service
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
