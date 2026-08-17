# -*- coding: utf-8 -*-
"""Knowledge-base embedding options sourced from Raven's own config.

AgentScope picks a knowledge base's embedding model by walking the caller's
AgentScope credentials and asking each provider class for its model cards.
Neither half holds anything in this deployment: Raven users configure providers
in Raven's config, not through ``/credential``, and the adopted tree ships no
``_models/*.yaml``, so ``list_models()`` returns an empty list for every
provider. The picker was therefore always empty and no knowledge base could be
created at all.

Rather than reviving the card YAML, take the model the operator already
configured for EverOS memory (``~/.everos/raven/everos.toml``, ``[embedding]``):
it is a working OpenAI-compatible endpoint with a key, which is exactly what a
knowledge base needs. It is registered as an ``openai_credential`` so the rest
of the AgentScope machinery -- indexing, query-time embedding -- keeps working
unchanged, and the picker is served from the same source.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from agentscope.app._router._schema._knowledge_base import (
    KbEmbeddingProvider,
    ListKbEmbeddingModelsResponse,
)
from agentscope.app._service import CredentialView
from agentscope.app.rag.knowledge_base_manager import DimensionPolicy, DimensionPolicyKind
from agentscope.credential import OpenAICredential
from agentscope.embedding import EmbeddingModelCard
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")

# EverOS requires 1024-dim vectors and raven's onboard wizard verifies that
# before writing the section, so a model reaching us here already satisfies it.
_EMBEDDING_DIMENSIONS = 1024
_CREDENTIAL_NAME = "raven-everos-embedding"


def _everos_config_path() -> Path:
    """Where raven keeps the EverOS config.

    Resolved here rather than through ``raven.config.update_everos``: that
    module imports ``tomli_w`` for its write path, which the service env does
    not carry, and reading needs nothing beyond the standard library.
    ``EVEROS_ROOT`` is the same override raven itself honours.
    """
    root = os.environ.get("EVEROS_ROOT") or "~/.everos/raven"
    return Path(root).expanduser() / "everos.toml"


def _everos_embedding() -> dict[str, Any]:
    """The ``[embedding]`` section of the EverOS config, or ``{}``."""
    path = _everos_config_path()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return dict(tomllib.load(fh).get("embedding") or {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning("knowledge: cannot read %s: %s", path, exc)
        return {}


def _card(model: str) -> EmbeddingModelCard:
    return EmbeddingModelCard(
        name=model,
        label=model,
        status="active",
        input_types=["text"],
        output_types=["vector"],
        dimensions=_EMBEDDING_DIMENSIONS,
    )


async def ensure_embedding_credential(storage: Any, user_id: str) -> str | None:
    """Mirror the EverOS embedding model into an AgentScope credential.

    Returns the credential id, or None when EverOS has no usable embedding
    section.

    The record tracks EverOS rather than snapshotting it: rotating the key
    there would otherwise leave every existing knowledge base authenticating
    with the old one, since indexing and query-time embedding both resolve
    through this credential. The id is reused on update, because knowledge
    bases store it and a fresh one would orphan them. Only a real difference
    is written, so the common path stays a single read.
    """
    section = _everos_embedding()
    api_key, base_url = section.get("api_key"), section.get("base_url")
    if not (api_key and base_url and section.get("model")):
        return None
    try:
        for record in await storage.list_credentials(user_id):
            data = record.data or {}
            if data.get("name") != _CREDENTIAL_NAME:
                continue
            if data.get("api_key") == api_key and data.get("base_url") == base_url:
                return record.id
            logger.info("knowledge: refreshing the embedding credential from EverOS")
            return await storage.upsert_credential(
                user_id,
                OpenAICredential(
                    id=record.id,
                    api_key=api_key,
                    base_url=base_url,
                    name=_CREDENTIAL_NAME,
                ),
            )
        return await storage.upsert_credential(
            user_id,
            OpenAICredential(api_key=api_key, base_url=base_url, name=_CREDENTIAL_NAME),
        )
    except Exception as exc:  # storage down / schema drift
        logger.warning("knowledge: could not register the embedding credential: %s", exc)
        return None


async def _picker_payload(storage: Any, user_id: str) -> dict:
    """The embedding picker AgentScope would have served, built from Raven."""
    policy = {"kind": "any", "dimension": None}
    section = _everos_embedding()
    model = section.get("model")
    credential_id = await ensure_embedding_credential(storage, user_id)
    if not (model and credential_id):
        # An empty list is the honest answer; the page renders its own
        # "configure an embedding model first" state from it.
        return {"providers": [], "policy": policy}

    view = CredentialView(
        id=credential_id,
        user_id=user_id,
        data={"type": "openai_credential", "name": _CREDENTIAL_NAME},
        editable=False,
    )
    payload = ListKbEmbeddingModelsResponse(
        providers=[KbEmbeddingProvider(credential=view, models=[_card(model)])],
        policy=DimensionPolicy(kind=DimensionPolicyKind.ANY, dimension=None),
    )
    return payload.model_dump(mode="json")


def install_kb_embedding_override(app: Any, storage: Any) -> None:
    """Serve ``/knowledge_bases/embedding_models`` from Raven's config.

    Done as middleware because replacing the route is not available here, which is
    worth writing down precisely: this comment has already been wrong twice, in
    both directions.

    ``create_app`` does include its routers synchronously in its own body, so
    "they are registered later, during lifespan startup" -- the first version of
    this note -- was false. But the route still cannot be found and swapped.
    FastAPI 0.139 keeps each included router as an opaque ``_IncludedRouter``
    entry and never flattens it, so scanning ``app.router.routes`` for the path
    matches nothing; the only public way through is
    ``_IncludedRouter.original_router``, which is the module-level
    ``knowledge_base_router`` singleton. Editing that mutates a shared object
    every app including it would see, which is worse than what this avoids.

    So the two costs here are deliberate, not deferred: the original handler runs
    on every request and its answer is discarded, and because middleware sits
    outside ``CORSMiddleware`` the access-control headers are copied off that
    discarded response by hand.
    """
    target = "/knowledge_bases/embedding_models"

    @app.middleware("http")
    async def _kb_embedding_models(request, call_next):  # type: ignore[no-untyped-def]
        if request.method != "GET" or request.url.path.rstrip("/") != target:
            return await call_next(request)
        user_id = request.headers.get("X-User-ID")
        if not user_id:
            return await call_next(request)
        # Run the original first and keep its headers: this middleware sits
        # outside the CORS one, so a response built here from scratch would
        # reach the browser without the access-control headers every other
        # endpoint gets, and the fetch would be blocked.
        downstream = await call_next(request)
        try:
            payload = await _picker_payload(storage, user_id)
        except Exception as exc:  # noqa: BLE001 - leave the original answer alone
            logger.warning("knowledge: embedding picker failed, deferring: %s", exc)
            return downstream
        passthrough = {k: v for k, v in downstream.headers.items() if k.lower().startswith("access-control-")}
        return JSONResponse(payload, status_code=200, headers=passthrough)
