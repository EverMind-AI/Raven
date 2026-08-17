# -*- coding: utf-8 -*-
"""Read one knowledge document's stored text back for preview.

AgentScope keeps the uploaded bytes in the blob store for the lifetime of the
document (they are only dropped when the document is deleted), but exposes no
way to read them: the document routes cover upload, list, status and delete.
So the UI could show that a file had been indexed and not what was in it, which
is the first thing anyone asks when a chunk looks wrong.

Only a bounded head of the file is returned, and only decoded as text -- these
knowledge bases accept text formats exclusively, and a preview does not need to
be a download.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

logger = logging.getLogger("uvicorn.error")

# Enough to read a document and judge it, small enough that the browser is not
# handed a whole book over one request.
_PREVIEW_LIMIT_BYTES = 256 * 1024


def build_kb_preview_router(storage: Any, user_id_dep: Any, blob_store_dep: Any) -> APIRouter:
    """A read-only preview route, mounted alongside AgentScope's own.

    The path is new rather than a replacement, so plain registration is enough;
    nothing here shadows an existing handler. The blob store arrives through
    AgentScope's own dependency so this reads the very store the indexer wrote
    to, rather than a second handle on the same directory.
    """
    router = APIRouter(prefix="/knowledge_bases", tags=["knowledge-base"])

    @router.get("/{knowledge_base_id}/documents/{document_id}/content")
    async def read_document_content(
        knowledge_base_id: str,
        document_id: str,
        user_id: str = Depends(user_id_dep),
        blob_store: Any = Depends(blob_store_dep),
    ) -> dict:
        record = await storage.get_knowledge_document(user_id, knowledge_base_id, document_id)
        if record is None:
            raise HTTPException(status_code=404, detail="document not found")
        data = record.data
        try:
            async with blob_store.open(data.blob_uri) as fh:
                payload = await fh.read(_PREVIEW_LIMIT_BYTES + 1)
        except Exception as exc:  # blob swept / store unavailable
            logger.warning("knowledge: cannot read %s: %s", data.blob_uri, exc)
            raise HTTPException(status_code=404, detail="document bytes are no longer available")

        truncated = len(payload) > _PREVIEW_LIMIT_BYTES
        # errors="replace" rather than a hard failure: a file that survived
        # parsing is worth showing even if a stray byte does not decode.
        text = payload[:_PREVIEW_LIMIT_BYTES].decode("utf-8", errors="replace")
        return {
            "document_id": document_id,
            "filename": data.filename,
            "content_type": data.content_type,
            "size": data.size,
            "truncated": truncated,
            "content": text,
        }

    return router
